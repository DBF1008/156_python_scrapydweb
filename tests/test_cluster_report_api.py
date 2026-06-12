# coding: utf-8
from flask import url_for

from tests.utils import cst, req


# -- Validation / error-path tests ---------------------------------------

def test_cluster_report_api_no_nodes(app, client):
    """Requesting without selecting any nodes must return 400."""
    req(app, client,
        view='clusterreportapi',
        kws=dict(node=1, project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID),
        jskws=dict(status='error'),
        ins=['No nodes selected'])


def test_cluster_report_api_invalid_nodes(app, client):
    """Node index outside [1, SCRAPYD_SERVERS_AMOUNT] must return 400."""
    with app.test_request_context():
        url = url_for('clusterreportapi', node=1,
                      project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID,
                      nodes='99')
    req(app, client, url=url,
        jskws=dict(status='error'),
        ins=['Invalid node index'])


def test_cluster_report_api_invalid_nodes_non_numeric(app, client):
    """Non-numeric node values must return 400."""
    with app.test_request_context():
        url = url_for('clusterreportapi', node=1,
                      project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID,
                      nodes='abc')
    req(app, client, url=url,
        jskws=dict(status='error'),
        ins=['Invalid node index'])


# -- Structure tests ------------------------------------------------------

def test_cluster_report_api_single_node_structure(app, client):
    """Single-node request returns valid JSON with the expected keys."""
    data = {'1': 'on'}
    text, js = req(app, client,
                   view='clusterreportapi',
                   kws=dict(node=1, project=cst.PROJECT,
                            spider=cst.SPIDER, job=cst.JOBID),
                   data=data,
                   jskws=dict(status='ok'))

    # Top-level keys
    for key in ('project', 'spider', 'job', 'selected_nodes',
                'summary', 'node_results', 'url_cluster_reports', 'when'):
        assert key in js, "Missing top-level key: %s" % key

    # Exactly one node result
    assert len(js['node_results']) == 1
    assert js['selected_nodes'] == [1]

    # Node result structure
    nr = js['node_results'][0]
    for key in ('node', 'node_server', 'node_state'):
        assert key in nr, "Missing node_result key: %s" % key
    assert nr['node'] == 1
    assert nr['node_state'] in ('finished', 'running', 'error')
    if nr['node_state'] == 'error':
        assert nr['report'] is None
        assert 'error_message' in nr
    else:
        assert nr['report'] is not None
        assert nr['report']['status'] == 'ok'


def test_cluster_report_api_fake_node_error(app, client):
    """Unreachable node (fake server at index 2) is classified as error."""
    data = {'2': 'on'}
    text, js = req(app, client,
                   view='clusterreportapi',
                   kws=dict(node=1, project=cst.PROJECT,
                            spider=cst.SPIDER, job=cst.JOBID),
                   data=data,
                   jskws=dict(status='ok'))

    assert len(js['node_results']) == 1
    nr = js['node_results'][0]
    assert nr['node'] == 2
    assert nr['node_state'] == 'error'
    assert nr['report'] is None
    assert 'error_message' in nr

    assert js['summary']['error_amount'] == 1
    assert js['summary']['finished_amount'] == 0
    assert js['summary']['running_amount'] == 0


def test_cluster_report_api_mixed_nodes(app, client):
    """Mixing a real node (1) with a fake node (2) produces correct
    per-node results and summary counts."""
    data = {'1': 'on', '2': 'on'}
    text, js = req(app, client,
                   view='clusterreportapi',
                   kws=dict(node=1, project=cst.PROJECT,
                            spider=cst.SPIDER, job=cst.JOBID),
                   data=data,
                   jskws=dict(status='ok'))

    assert js['selected_nodes'] == [1, 2]
    assert len(js['node_results']) == 2

    # Node 1 (real server): any valid state is acceptable
    nr1 = js['node_results'][0]
    assert nr1['node'] == 1
    assert nr1['node_state'] in ('finished', 'running', 'error')

    # Node 2 (fake server): must always be error
    nr2 = js['node_results'][1]
    assert nr2['node'] == 2
    assert nr2['node_state'] == 'error'

    # Summary arithmetic must be consistent
    summary = js['summary']
    assert summary['error_amount'] >= 1
    assert (summary['finished_amount']
            + summary['running_amount']
            + summary['error_amount']) == 2


# -- Input-format tests ---------------------------------------------------

def test_cluster_report_api_nodes_query_param(app, client):
    """``nodes=1,2`` passed as a query-string parameter works."""
    with app.test_request_context():
        url = url_for('clusterreportapi', node=1,
                      project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID,
                      nodes='1,2')
    text, js = req(app, client, url=url, jskws=dict(status='ok'))
    assert js['selected_nodes'] == [1, 2]
    assert len(js['node_results']) == 2


def test_cluster_report_api_nodes_form_post(app, client):
    """``nodes=1,2`` passed as a POST form field works."""
    data = {'nodes': '1,2'}
    text, js = req(app, client,
                   view='clusterreportapi',
                   kws=dict(node=1, project=cst.PROJECT,
                            spider=cst.SPIDER, job=cst.JOBID),
                   data=data,
                   jskws=dict(status='ok'))
    assert js['selected_nodes'] == [1, 2]
    assert len(js['node_results']) == 2


def test_cluster_report_api_deduplicate_nodes(app, client):
    """Duplicate node indices are silently deduplicated."""
    with app.test_request_context():
        url = url_for('clusterreportapi', node=1,
                      project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID,
                      nodes='1,1,2,2')
    text, js = req(app, client, url=url, jskws=dict(status='ok'))
    assert js['selected_nodes'] == [1, 2]
    assert len(js['node_results']) == 2


# -- Summary / aggregation tests -----------------------------------------

def test_cluster_report_api_summary_keys(app, client):
    """All expected summary keys are present and hold integer values."""
    data = {'1': 'on'}
    text, js = req(app, client,
                   view='clusterreportapi',
                   kws=dict(node=1, project=cst.PROJECT,
                            spider=cst.SPIDER, job=cst.JOBID),
                   data=data,
                   jskws=dict(status='ok'))

    expected_keys = [
        'total_pages', 'total_items',
        'total_log_critical', 'total_log_error', 'total_log_warning',
        'total_log_redirect', 'total_log_retry', 'total_log_ignore',
        'finished_amount', 'running_amount', 'error_amount',
    ]
    for key in expected_keys:
        assert key in js['summary'], "Missing summary key: %s" % key
        assert isinstance(js['summary'][key], int), \
            "Summary key %s should be int, got %s" % (key, type(js['summary'][key]))

    # State counts must add up to total nodes
    s = js['summary']
    assert s['finished_amount'] + s['running_amount'] + s['error_amount'] == 1
