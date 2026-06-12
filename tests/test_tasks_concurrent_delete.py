# coding: utf-8
"""
Regression tests for the concurrent-deletion race between the apscheduler executor thread
(scrapydweb/views/operations/execute_task.py) and the Timer Tasks delete paths
(scrapydweb/views/overview/tasks.py).

Before the fix, deleting a task or a task_result from the Timer Tasks page while a task was
executing could leave orphan ``task_job_result`` rows (rows whose ``task_result_id`` no longer
exists) or wrong FAIL/PASS counts, because the executor checked existence and then committed in a
separate, uncoordinated transaction. The fix serializes the executor's short DB critical sections
against the delete paths with ``scrapydweb.models.task_result_lock`` and re-checks existence inside
the lock.

These tests do NOT need a running scrapyd: ``TaskExecutor.schedule_task`` is replaced with a canned
result, and interleavings are forced deterministically with ``threading.Event`` (no sleeps).
"""
# --- Python 3.12+ compatibility shim (test-only, no-op on the interpreter this project targets) ---
# The project pins Werkzeug 2.0, whose URL-rule compiler still uses ast.Str (removed in Py 3.12).
# Without this, Flask(__name__) cannot construct a routing map, so neither this file nor the rest of
# the existing test suite can build an app on a newer interpreter. Guarded so it does nothing on the
# older Python the project actually runs on.
import ast as _ast
if not hasattr(_ast, 'Str'):
    class _Str(_ast.Constant):
        def __init__(self, s='', value=None, **kwargs):
            super(_Str, self).__init__(value=s if value is None else value, **kwargs)

        @property
        def s(self):
            return self.value
    _ast.Str = _Str
# ---------------------------------------------------------------------------------------------------

import threading

from flask import url_for

from scrapydweb.models import db, Task, TaskResult, TaskJobResult, task_result_lock
from scrapydweb.views.operations.execute_task import TaskExecutor


URL_DELETE_TASK_RESULT = '/1/tasks/xhr/delete/1/1/'  # template, ids get rewritten by the executor


def _make_task(name, selected_nodes='[1, 2]'):
    """Insert a minimal Task row directly and return its id."""
    task = Task()
    task.name = name
    task.trigger = 'cron'
    task.project = 'demo'
    task.version = 'v1'
    task.spider = 'test'
    task.jobid = 'jobid'
    task.settings_arguments = '{}'
    task.selected_nodes = selected_nodes
    task.year = task.month = task.day = task.week = task.day_of_week = '*'
    task.hour = task.minute = task.second = '*'
    task.jitter = 0
    task.coalesce = 'True'
    task.max_instances = 1
    db.session.add(task)
    db.session.commit()
    return task.id


def _ok_result(node, task_id):
    """A canned 'ok' schedule_task() result, as db_insert_task_job_result() expects it."""
    return dict(node=node, url='http://127.0.0.1:6800', status_code=200, status='ok',
                jobid='task_%s_job_node%s' % (task_id, node))


def _make_executor(task_id, selected_nodes):
    executor = TaskExecutor(task_id=task_id, task_name='t%s' % task_id,
                            url_scrapydweb='http://127.0.0.1:5000',
                            url_schedule_task='/1/schedule/task/',
                            url_delete_task_result=URL_DELETE_TASK_RESULT,
                            auth=None, selected_nodes=selected_nodes)
    executor.schedule_task = lambda node: _ok_result(node, task_id)
    return executor


def _count_orphans():
    """Number of task_job_result rows whose task_result_id no longer exists."""
    db.session.expire_all()
    valid_ids = set(tr.id for tr in TaskResult.query.all())
    return sum(1 for tjr in TaskJobResult.query.all() if tjr.task_result_id not in valid_ids)


def _delete_via_view(app, **kwargs):
    """Hit the real TasksXhrView delete endpoint through the test client (separate request)."""
    with app.test_request_context():
        url = url_for('tasks.xhr', node=1, action='delete', **kwargs)
    resp = app.test_client().post(url)
    assert resp.status_code == 200
    return resp


# ---------------------------------------------------------------------------------------------------
# 1. White-box: if the task_result is gone by the time db_insert_task_job_result runs (the deleter
#    already committed), the re-check inside task_result_lock must discard the insert -> no orphan.
# ---------------------------------------------------------------------------------------------------
def test_insert_discarded_when_task_result_already_deleted(app):
    with app.app_context():
        task_id = _make_task('white-box-recheck')
        executor = _make_executor(task_id, selected_nodes=[1])
        executor.get_task_result_id()
        trid = executor.task_result_id
        assert trid is not None

        # Simulate the concurrent deleter having committed before the insert runs.
        db.session.delete(TaskResult.query.get(trid))
        db.session.commit()

        executor.db_insert_task_job_result(_ok_result(1, task_id))

        assert TaskJobResult.query.filter_by(task_result_id=trid).count() == 0
        assert _count_orphans() == 0


# ---------------------------------------------------------------------------------------------------
# 2. End-to-end: delete a task_result through the real view while the task is mid-execution.
#    The executor must observe the deletion (re-check) and leave no orphan task_job_result; the
#    deleted task_result must stay deleted.
# ---------------------------------------------------------------------------------------------------
def test_delete_task_result_during_execution(app):
    with app.app_context():
        task_id = _make_task('delete-task-result-mid-run')
        executor = _make_executor(task_id, selected_nodes=[1, 2])

        node1_inserted = threading.Event()
        proceed = threading.Event()

        real_insert = executor.db_insert_task_job_result
        state = {'calls': 0}

        def gated_insert(js):
            real_insert(js)                 # node 1 commits its task_job_result (lock released after)
            state['calls'] += 1
            if state['calls'] == 1:
                node1_inserted.set()        # tell the main thread node 1 is committed
                assert proceed.wait(10)     # pause before node 2 so the delete can land in between

        executor.db_insert_task_job_result = gated_insert

        worker = threading.Thread(target=executor.main)
        worker.start()
        try:
            assert node1_inserted.wait(10)
            trid = executor.task_result_id
            assert trid is not None
            assert TaskJobResult.query.filter_by(task_result_id=trid).count() == 1

            # Delete the task_result via the real endpoint, then let the executor continue to node 2.
            _delete_via_view(app, task_id=task_id, task_result_id=trid)
        finally:
            proceed.set()
            worker.join(15)
        assert not worker.is_alive()

        db.session.expire_all()
        # The deleted task_result stays deleted; node 1's row went with it (cascade) and node 2's
        # insert was discarded by the re-check -> zero rows, zero orphans.
        assert TaskResult.query.get(trid) is None
        assert TaskJobResult.query.filter_by(task_result_id=trid).count() == 0
        assert _count_orphans() == 0
        # The task itself is untouched.
        assert Task.query.get(task_id) is not None


# ---------------------------------------------------------------------------------------------------
# 3. End-to-end: delete the whole task through the real view while it is mid-execution. The finalize
#    self-heal (task gone -> delete the stale task_result via get_response_from_view, which re-enters
#    the delete view in this same thread) must complete without deadlock and leave no orphans.
# ---------------------------------------------------------------------------------------------------
def test_delete_task_during_execution(app):
    with app.app_context():
        task_id = _make_task('delete-task-mid-run')
        executor = _make_executor(task_id, selected_nodes=[1, 2])

        node1_inserted = threading.Event()
        proceed = threading.Event()

        real_insert = executor.db_insert_task_job_result
        state = {'calls': 0}

        def gated_insert(js):
            real_insert(js)
            state['calls'] += 1
            if state['calls'] == 1:
                node1_inserted.set()
                assert proceed.wait(10)

        executor.db_insert_task_job_result = gated_insert

        worker = threading.Thread(target=executor.main)
        worker.start()
        try:
            assert node1_inserted.wait(10)
            trid = executor.task_result_id
            assert trid is not None

            # Delete the whole task (apscheduler_job is None for a directly-created Task row).
            _delete_via_view(app, task_id=task_id)
        finally:
            proceed.set()
            worker.join(15)
        assert not worker.is_alive()

        db.session.expire_all()
        assert Task.query.get(task_id) is None
        assert TaskResult.query.get(trid) is None
        assert TaskJobResult.query.filter_by(task_result_id=trid).count() == 0
        assert _count_orphans() == 0


# ---------------------------------------------------------------------------------------------------
# 4. Sanity: an uninterrupted run still produces the right task_result counts and exactly the
#    expected task_job_result rows (the lock did not change normal behavior).
# ---------------------------------------------------------------------------------------------------
def test_normal_execution_unaffected(app):
    with app.app_context():
        task_id = _make_task('normal-path')
        executor = _make_executor(task_id, selected_nodes=[1, 2])

        executor.main()

        trid = executor.task_result_id
        assert trid is not None
        db.session.expire_all()
        task_result = TaskResult.query.get(trid)
        assert task_result is not None
        assert task_result.pass_count == 2
        assert task_result.fail_count == 0
        assert TaskJobResult.query.filter_by(task_result_id=trid).count() == 2
        assert _count_orphans() == 0


# ---------------------------------------------------------------------------------------------------
# 5. Edge: the task is deleted before the execution can even create its task_result. get_task_result_id
#    must skip creation (no orphan task_result) and main() must be a no-op.
# ---------------------------------------------------------------------------------------------------
def test_task_deleted_before_task_result_created(app):
    with app.app_context():
        task_id = _make_task('deleted-before-start')
        # Delete the task up front, then run the executor.
        _delete_via_view(app, task_id=task_id)
        assert Task.query.get(task_id) is None

        executor = _make_executor(task_id, selected_nodes=[1, 2])
        executor.main()

        assert executor.task_result_id is None
        assert TaskResult.query.filter_by(task_id=task_id).count() == 0
        assert _count_orphans() == 0


# ---------------------------------------------------------------------------------------------------
# The following three tests pin the actual coordination primitive: each guarded critical section must
# wait on scrapydweb.models.task_result_lock. They hold the lock from the test thread and assert the
# section cannot make progress until it is released. These FAIL on the pre-fix code (no lock there),
# which is exactly the race that produced orphan task_job_result rows / wrong counts.
#
# Holding only the Python lock (no DB transaction) avoids any SQLite write-lock confound: a blocked
# section is blocked on the lock itself, before it touches the database.
# ---------------------------------------------------------------------------------------------------
def _spawn(target):
    done = threading.Event()

    def runner():
        try:
            target()
        finally:
            done.set()

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, done


def test_delete_task_result_respects_lock(app):
    with app.app_context():
        task_id = _make_task('delete-task-result-respects-lock')
        executor = _make_executor(task_id, selected_nodes=[1])
        executor.get_task_result_id()
        trid = executor.task_result_id
        assert trid is not None

        task_result_lock.acquire()
        thread, done = _spawn(lambda: _delete_via_view(app, task_id=task_id, task_result_id=trid))
        try:
            # delete_task_result() must block on task_result_lock; it cannot finish while we hold it.
            assert not done.wait(0.5), "delete_task_result did not wait on task_result_lock"
        finally:
            task_result_lock.release()
        assert done.wait(10)
        thread.join(10)
        assert not thread.is_alive()

        db.session.expire_all()
        assert TaskResult.query.get(trid) is None
        assert _count_orphans() == 0


def test_delete_task_respects_lock(app):
    with app.app_context():
        task_id = _make_task('delete-task-respects-lock')
        executor = _make_executor(task_id, selected_nodes=[1])
        executor.get_task_result_id()
        assert executor.task_result_id is not None

        task_result_lock.acquire()
        thread, done = _spawn(lambda: _delete_via_view(app, task_id=task_id))
        try:
            # delete_task() must block on task_result_lock around the DB delete.
            assert not done.wait(0.5), "delete_task did not wait on task_result_lock"
        finally:
            task_result_lock.release()
        assert done.wait(10)
        thread.join(10)
        assert not thread.is_alive()

        db.session.expire_all()
        assert Task.query.get(task_id) is None
        assert _count_orphans() == 0


def test_insert_respects_lock(app):
    with app.app_context():
        task_id = _make_task('insert-respects-lock')
        executor = _make_executor(task_id, selected_nodes=[1])
        executor.get_task_result_id()       # acquires/releases the lock itself -> do it before holding
        trid = executor.task_result_id
        assert trid is not None

        task_result_lock.acquire()
        thread, done = _spawn(lambda: executor.db_insert_task_job_result(_ok_result(1, task_id)))
        try:
            # db_insert_task_job_result() must block on task_result_lock; no row may appear yet.
            assert not done.wait(0.5), "db_insert_task_job_result did not wait on task_result_lock"
            db.session.expire_all()
            assert TaskJobResult.query.filter_by(task_result_id=trid).count() == 0
        finally:
            task_result_lock.release()
        assert done.wait(10)
        thread.join(10)
        assert not thread.is_alive()

        db.session.expire_all()
        assert TaskJobResult.query.filter_by(task_result_id=trid).count() == 1
        assert _count_orphans() == 0
