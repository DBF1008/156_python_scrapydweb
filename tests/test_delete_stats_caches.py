# coding: utf-8
"""Regression tests for clearing cached results when a finished job is deleted.

Bug: opening the Stats/Report page of a finished job caches its parsed result in two
places -- the in-memory ``job_finished_report_dict`` (served by the Reports page) and an
on-disk backup stats json file (served as a fallback by the Stats page when the scrapy
logfile is missing). Deleting the job from the Jobs page (database view) used to leave both
behind, so the Reports page and the stats fallback kept returning stale results for that
run; with many nodes this made new and old jobs easy to confuse.

Fix: ``JobsXhrView`` now purges both caches for finished jobs via
``scrapydweb.views.files.log.delete_stats_caches``, while leaving running/pending jobs
untouched so their database-driven recovery semantics and the live backup fallback stay
intact.
"""
import io
import os
import re

from scrapydweb.models import create_jobs_table, db
from scrapydweb.vars import LEGAL_NAME_PATTERN, STATS_PATH, STRICT_NAME_PATTERN, jobs_table_map
from scrapydweb.views.files.log import LogView, delete_stats_caches, job_finished_report_dict
from tests.utils import cst, req


STATUS_RUNNING = '1'
STATUS_FINISHED = '2'
NOT_DELETED = '0'
DELETED = '1'


def _write_backup_stats(path):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(u'{"status": "ok", "finish_reason": "finished"}')


# --------------------------------------------------------------------------- #
# Pure-function level: delete_stats_caches() and the backup-path helper.
# --------------------------------------------------------------------------- #
def test_get_backup_stats_path_matches_node_dir_naming():
    # Pin the backup path to the same convention used by LogView.mkdir_spider_path() and
    # tests.utils.setup_env(), i.e. '127.0.0.1:6800' -> '127_0_0_1_6800'. If this naming
    # ever drifts, JobsXhrView would try to delete the wrong file and the bug would return.
    path = LogView.get_backup_stats_path(STATS_PATH, '127.0.0.1:6800', LEGAL_NAME_PATTERN,
                                         cst.PROJECT, cst.SPIDER, cst.JOBID)
    expected = os.path.join(STATS_PATH, '127_0_0_1_6800', cst.PROJECT, cst.SPIDER, cst.JOBID + '.json')
    assert path == expected


def test_delete_stats_caches_purges_report_cache_and_backup_file(tmp_path):
    node = 99  # an isolated node index so we never disturb cache entries used by other tests
    project, spider, job = 'delete_cache_proj', 'spider', 'jobid_pure'
    job_key = '/%s/%s/%s/%s' % (node, project, spider, job)
    backup_path = os.path.join(str(tmp_path), 'node', project, spider, job + '.json')

    job_finished_report_dict[node][job_key] = {'status': 'ok', 'finish_reason': 'finished'}
    _write_backup_stats(backup_path)
    assert job_key in job_finished_report_dict[node]
    assert os.path.isfile(backup_path)

    try:
        # Both the in-memory report cache entry and the on-disk backup file are removed.
        assert delete_stats_caches(node, project, spider, job, backup_stats_path=backup_path) == (True, True)
        assert job_key not in job_finished_report_dict[node]
        assert not os.path.exists(backup_path)

        # Idempotent: deleting again is a harmless no-op (nothing left to remove).
        assert delete_stats_caches(node, project, spider, job, backup_stats_path=backup_path) == (False, False)
    finally:
        job_finished_report_dict.pop(node, None)


def test_delete_stats_caches_does_not_materialize_unknown_node():
    # An unknown node must not be created as an empty entry by the defaultdict, otherwise
    # repeated deletes would slowly leak per-node dicts.
    node = 12345
    assert node not in job_finished_report_dict
    assert delete_stats_caches(node, 'p', 's', 'j') == (False, False)
    assert node not in job_finished_report_dict


# --------------------------------------------------------------------------- #
# Endpoint level: the real /<node>/jobs/xhr/delete/<id>/ delete chain.
# --------------------------------------------------------------------------- #
def _ensure_jobs_table(app, node):
    # Mirror JobsView.create_table(): register the per-node Job table once and reuse it.
    scrapyd_server = app.config['SCRAPYD_SERVERS'][node - 1]
    Job = jobs_table_map.get(node)
    if Job is None:
        Job = create_jobs_table(re.sub(STRICT_NAME_PATTERN, '_', scrapyd_server))
        db.create_all(bind='jobs')
        jobs_table_map[node] = Job
    return Job, scrapyd_server


def _insert_job(Job, project, spider, job, status):
    record = Job(project=project, spider=spider, job=job, status=status, deleted=NOT_DELETED)
    db.session.add(record)
    db.session.commit()
    return record.id


def _delete_row(app, Job, job_id):
    with app.app_context():
        record = Job.query.get(job_id)
        if record is not None:
            db.session.delete(record)
            db.session.commit()


def test_jobs_xhr_delete_finished_purges_caches(app, client):
    node = 1
    project, spider, job = 'delete_cache_finished', 'test', '2018-01-01T01_01_99'
    job_key = '/%s/%s/%s/%s' % (node, project, spider, job)

    with app.app_context():
        Job, scrapyd_server = _ensure_jobs_table(app, node)
        job_id = _insert_job(Job, project, spider, job, STATUS_FINISHED)

    backup_path = LogView.get_backup_stats_path(STATS_PATH, scrapyd_server, LEGAL_NAME_PATTERN,
                                                project, spider, job)
    job_finished_report_dict[node][job_key] = {'status': 'ok', 'finish_reason': 'finished'}
    _write_backup_stats(backup_path)
    assert job_key in job_finished_report_dict[node]
    assert os.path.isfile(backup_path)

    try:
        # Delete the finished job through the real XHR endpoint (the delete chain under test).
        req(app, client, view='jobs.xhr', kws=dict(node=node, action='delete', id=job_id),
            jskws=dict(status=cst.OK))

        # The Reports cache entry and the stats backup file must both be gone now.
        assert job_key not in job_finished_report_dict[node]
        assert not os.path.exists(backup_path)

        # The database record itself is soft-deleted (the row is kept, flagged deleted).
        with app.app_context():
            assert Job.query.get(job_id).deleted == DELETED
    finally:
        job_finished_report_dict.pop(node, None)
        try:
            os.remove(backup_path)
        except OSError:
            pass
        _delete_row(app, Job, job_id)


def test_jobs_xhr_delete_running_preserves_backup_fallback(app, client):
    node = 1
    project, spider, job = 'delete_cache_running', 'test', '2018-01-01T01_01_98'

    with app.app_context():
        Job, scrapyd_server = _ensure_jobs_table(app, node)
        job_id = _insert_job(Job, project, spider, job, STATUS_RUNNING)

    backup_path = LogView.get_backup_stats_path(STATS_PATH, scrapyd_server, LEGAL_NAME_PATTERN,
                                                project, spider, job)
    _write_backup_stats(backup_path)
    assert os.path.isfile(backup_path)

    try:
        req(app, client, view='jobs.xhr', kws=dict(node=node, action='delete', id=job_id),
            jskws=dict(status=cst.OK))

        # A running job is soft-deleted in the database (so JobsView.db_insert_jobs can later
        # recover it), but its backup stats file is preserved as the live fallback -- only
        # finished jobs get their caches purged.
        assert os.path.isfile(backup_path)
        with app.app_context():
            assert Job.query.get(job_id).deleted == DELETED
    finally:
        try:
            os.remove(backup_path)
        except OSError:
            pass
        _delete_row(app, Job, job_id)
