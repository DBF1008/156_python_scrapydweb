# coding: utf-8
import json
import logging
import re
import threading
import time
import traceback

from ...common import get_now_string, get_response_from_view, handle_metadata
from ...models import Task, TaskResult, TaskJobResult, db
from ...utils.scheduler import scheduler


apscheduler_logger = logging.getLogger('apscheduler')

REPLACE_URL_NODE_PATTERN = re.compile(r'^/(\d+)/')
EXTRACT_URL_SERVER_PATTERN = re.compile(r'//(.+?:\d+)')


# ---------------------------------------------------------------------------
# In-process cancellation registry
# ---------------------------------------------------------------------------
# When a Task or TaskResult is deleted from the web UI while an executor
# thread is still running, the delete handler calls mark_task_cancelled()
# so the executor can notice and stop writing to the database.  This avoids
# orphan TaskJobResult rows and stale pass/fail counts that would otherwise
# appear when the executor's INSERT races with the cascade DELETE.
# ---------------------------------------------------------------------------
_cancel_lock = threading.Lock()
_cancelled_tasks = set()          # {task_id, ...}
_cancelled_task_results = set()   # {(task_id, task_result_id), ...}


def mark_task_cancelled(task_id, task_result_id=None):
    """Signal to any running executor that *task_id* (and optionally a
    specific *task_result_id*) has been deleted and should be abandoned."""
    with _cancel_lock:
        if task_result_id is None:
            # Whole task deleted — cancel everything for this task_id
            _cancelled_tasks.add(task_id)
        else:
            # Only a specific task_result deleted — cancel that result
            # without affecting the task itself (other results keep running)
            _cancelled_task_results.add((task_id, task_result_id))


def is_task_cancelled(task_id, task_result_id=None):
    """Return ``True`` if *task_id* (or the specific *task_result_id*)
    has been marked as cancelled."""
    with _cancel_lock:
        if task_id in _cancelled_tasks:
            return True
        if task_result_id is not None and (task_id, task_result_id) in _cancelled_task_results:
            return True
        return False


def clear_task_cancelled(task_id):
    """Remove all cancellation markers for *task_id*.

    Called at the beginning of ``execute_task()`` so that a stale marker
    left by a previous run does not interfere with a fresh execution.
    """
    with _cancel_lock:
        _cancelled_tasks.discard(task_id)
        to_remove = [k for k in _cancelled_task_results if k[0] == task_id]
        for k in to_remove:
            _cancelled_task_results.discard(k)


class TaskExecutor(object):

    def __init__(self, task_id, task_name, url_scrapydweb, url_schedule_task, url_delete_task_result,
                 auth, selected_nodes):
        self.task_id = task_id
        self.task_name = task_name
        self.url_scrapydweb = url_scrapydweb
        self.url_schedule_task = url_schedule_task
        self.url_delete_task_result = url_delete_task_result
        self.auth = auth
        self.data = dict(
            task_id=task_id,
            jobid='task_%s_%s' % (task_id, get_now_string(allow_space=False))
        )
        self.selected_nodes = selected_nodes
        self.task_result_id = None  # Be set in get_task_result_id()
        self.pass_count = 0
        self.fail_count = 0

        self.sleep_seconds_before_retry = 3
        self.nodes_to_retry = []
        self.logger = logging.getLogger(self.__class__.__name__)

    # -- cancellation helpers ------------------------------------------------

    def _is_cancelled(self):
        """Check whether the current task or its result has been deleted
        from the web UI while this executor is still running."""
        return is_task_cancelled(self.task_id, self.task_result_id)

    # -- main flow -----------------------------------------------------------

    def main(self):
        self.get_task_result_id()
        for index, nodes in enumerate([self.selected_nodes, self.nodes_to_retry]):
            if not nodes:
                continue
            if index == 1:
                # Abort retry round if cancelled while we were sleeping
                if self._is_cancelled():
                    apscheduler_logger.warning(
                        "Task #%s (%s) cancelled before retry, aborting",
                        self.task_id, self.task_name)
                    break
                # https://apscheduler.readthedocs.io/en/latest/userguide.html#shutting-down-the-scheduler
                self.logger.warning("Retry task #%s (%s) on nodes %s in %s seconds",
                                    self.task_id, self.task_name, nodes, self.sleep_seconds_before_retry)
                time.sleep(self.sleep_seconds_before_retry)
                self.logger.warning("Retrying task #%s (%s) on nodes %s", self.task_id, self.task_name, nodes)
            for node in nodes:
                # Check cancellation before each node so we stop early
                if self._is_cancelled():
                    apscheduler_logger.warning(
                        "Task #%s (%s) cancelled during execution, aborting remaining nodes",
                        self.task_id, self.task_name)
                    break
                result = self.schedule_task(node)
                if result:
                    if result['status'] == 'ok':
                        self.pass_count += 1
                    else:
                        self.fail_count += 1
                    self.db_insert_task_job_result(result)
            else:
                # only continues outer loop when inner loop was NOT broken
                continue
            # inner loop was broken (cancelled) → also break outer loop
            break
        self.db_update_task_result()

    def get_task_result_id(self):
        # SQLite objects created in a thread can only be used in that same thread
        with db.app.app_context():
            task_result = TaskResult()
            task_result.task_id = self.task_id
            db.session.add(task_result)
            # db.session.flush()  # Get task_result.id before committing, flush() is part of commit()
            db.session.commit()
            # If directly use task_result.id later: Instance <TaskResult at 0x123> is not bound to a Session
            self.task_result_id = task_result.id
            self.logger.debug("Get new task_result_id %s for task #%s", self.task_result_id, self.task_id)

    def schedule_task(self, node):
        # TODO: Application was not able to create a URL adapter for request independent URL generation.
        # You might be able to fix this by setting the SERVER_NAME config variable.
        # with app.app_context():
        #     url_schedule_task = url_for('schedule.task', node=node)
        # http://127.0.0.1:5000/1/schedule/task/
        # /1/schedule/task/
        url_schedule_task = re.sub(REPLACE_URL_NODE_PATTERN, r'/%s/' % node, self.url_schedule_task)
        js = {}
        try:
            # assert '/1/' not in url_schedule_task, u"'故意出错'\r\n\"出错\"'故意出错'\r\n\"出错\""
            # assert False
            # time.sleep(10)
            js = get_response_from_view(url_schedule_task, auth=self.auth, data=self.data, as_json=True)
            assert js['status_code'] == 200 and js['status'] == 'ok', "Request got %s" % js
        except Exception as err:
            if node not in self.nodes_to_retry:
                apscheduler_logger.warning("Fail to execute task #%s (%s) on node %s, would retry later: %s",
                                           self.task_id, self.task_name, node, err)
                self.nodes_to_retry.append(node)
                return {}
            else:
                apscheduler_logger.error("Fail to execute task #%s (%s) on node %s, no more retries: %s",
                                         self.task_id, self.task_name, node, traceback.format_exc())
                js.setdefault('url', self.url_scrapydweb)  # '127.0.0.1:5000'
                js.setdefault('status_code', -1)
                js.setdefault('status', 'exception')
                js.setdefault('exception', traceback.format_exc())
        js.update(node=node)
        return js

    def db_insert_task_job_result(self, js):
        # Check cancellation BEFORE touching the database so we never
        # create an orphan TaskJobResult whose parent TaskResult was
        # cascade-deleted by a concurrent delete handler.
        if self._is_cancelled():
            apscheduler_logger.warning(
                "Task #%s or task_result #%s cancelled, discard task_job_result: %s",
                self.task_id, self.task_result_id, js)
            return

        with db.app.app_context():
            # Re-verify inside the app context: the cascade DELETE from
            # delete_task() / delete_task_result() may have committed
            # between our cancellation check above and this point.
            if not TaskResult.query.get(self.task_result_id):
                apscheduler_logger.error("task_result #%s of task #%s not found", self.task_result_id, self.task_id)
                apscheduler_logger.warning("Discard task_job_result of task_result #%s of task #%s: %s",
                                           self.task_result_id, self.task_id, js)
                return
            task_job_result = TaskJobResult()
            task_job_result.task_result_id = self.task_result_id
            task_job_result.node = js['node']
            task_job_result.server = re.search(EXTRACT_URL_SERVER_PATTERN, js['url']).group(1)  # '127.0.0.1:6800'
            task_job_result.status_code = js['status_code']
            task_job_result.status = js['status']
            task_job_result.result = js.get('jobid', '') or js.get('message', '') or js.get('exception', '')
            db.session.add(task_job_result)
            db.session.commit()
            self.logger.info("Inserted task_job_result: %s", task_job_result)

    # https://stackoverflow.com/questions/13895176/sqlalchemy-and-sqlite-database-is-locked
    def db_update_task_result(self):
        # If cancelled before we even created a TaskResult, nothing to do.
        if self.task_result_id is None:
            return

        with db.app.app_context():
            task = Task.query.get(self.task_id)
            task_result = TaskResult.query.get(self.task_result_id)

            if not task or self._is_cancelled():
                # The task was deleted (or marked cancelled) while we were
                # running.  Clean up the orphan TaskResult we created in
                # get_task_result_id() so the database stays consistent.
                apscheduler_logger.warning(
                    "Task #%s not found or cancelled, cleaning up orphan task_result #%s",
                    self.task_id, self.task_result_id)
                if task_result:
                    db.session.delete(task_result)
                    db.session.commit()
                return

            if not task_result:
                # The TaskResult was deleted concurrently (e.g. by
                # delete_task_result()).  Counts are lost; nothing we can
                # update, but this is NOT an error — the user explicitly
                # asked for the deletion.
                apscheduler_logger.warning(
                    "task_result #%s of task #%s was deleted during execution, "
                    "skip updating pass/fail counts [FAIL %s, PASS %s]",
                    self.task_result_id, self.task_id, self.fail_count, self.pass_count)
                return

            task_result.fail_count = self.fail_count
            task_result.pass_count = self.pass_count
            db.session.commit()
            self.logger.info("Inserted task_result: %s", task_result)


def execute_task(task_id):
    # Clear any stale cancellation marker left from a previous run of the
    # same task_id (the set is additive and never auto-cleaned otherwise).
    clear_task_cancelled(task_id)

    with db.app.app_context():
        task = Task.query.get(task_id)
        apscheduler_job = scheduler.get_job(str(task_id))
        if not task:
            if apscheduler_job:
                apscheduler_job.remove()
                apscheduler_logger.error("apscheduler_job #{id} removed since task #{id} not exist. "
                                         .format(id=task_id))
            else:
                apscheduler_logger.error("Task #{id} not exist and apscheduler_job #{id} already gone. "
                                         .format(id=task_id))
        else:
            metadata = handle_metadata()
            username = metadata.get('username', '')
            password = metadata.get('password', '')
            url_delete_task_result = metadata.get('url_delete_task_result', '/1/tasks/xhr/delete/1/1/')
            task_executor = TaskExecutor(task_id=task_id,
                                         task_name=task.name,
                                         url_scrapydweb=metadata.get('url_scrapydweb', 'http://127.0.0.1:5000'),
                                         url_schedule_task=metadata.get('url_schedule_task', '/1/schedule/task/'),
                                         url_delete_task_result=url_delete_task_result,
                                         auth=(username, password) if username and password else None,
                                         selected_nodes=json.loads(task.selected_nodes))
            try:
                task_executor.main()
            except Exception:
                apscheduler_logger.error(traceback.format_exc())
