# coding: utf-8
from flask import url_for

from tests.utils import cst, req, req_single_scrapyd


def test_node_reports_pass(app, client):
    with app.test_request_context():
        url_report = url_for('log', node=1, opt='report', project='PROJECT_PLACEHOLDER',
                             spider='SPIDER_PLACEHOLDER', job='JOB_PLACEHOLDER')
    ins = ["url_report: '%s'," % url_report, "start: '", "finish: '"]
    req(app, client, view='nodereports', kws=dict(node=1), ins=ins)
    req_single_scrapyd(app, client, view='nodereports', kws=dict(node=1), ins=ins)


def test_node_reports_fail(app, client):
    ins = ['<title>fail - ScrapydWeb</title>', '<h3>status_code: -1</h3>']
    req(app, client, view='nodereports', kws=dict(node=2), ins=ins)
    req_single_scrapyd(app, client, view='nodereports', kws=dict(node=1), ins=ins, set_to_second=True)


def test_cluster_reports(app, client):
    with app.test_request_context():
        url_servers = url_for('servers', node=2, opt='getreports', project=cst.PROJECT,
                              spider=cst.SPIDER, version_job=cst.JOBID)
        url_jobs = url_for('jobs', node=1)
        url_report = url_for('log', node=2, opt='report', project=cst.PROJECT,
                             spider=cst.SPIDER, job=cst.JOBID)
        url_redirect_to_clusterreports = url_for('clusterreports', node=1, project=cst.PROJECT,
                                                 spider=cst.SPIDER, job=cst.JOBID)
    ins = ['0 Reports of ////', '>Select a job</el-button>', url_jobs, 'selected_nodes: [],']
    nos = ['>Select nodes</el-button>']
    req(app, client, view='clusterreports', kws=dict(node=1), ins=ins)

    # Post from the servers page
    data = {
        '1': 'on',
        '2': 'on',
    }
    ins[0] = '%s Reports of /%s/%s/%s/' % (len(data), cst.PROJECT, cst.SPIDER, cst.JOBID)
    ins[-1] = 'selected_nodes: [1, 2],'
    ins.extend(nos)
    ins.append(url_servers)
    ins.append(url_report)
    kws = dict(node=2, project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID)
    req(app, client, view='clusterreports', kws=kws, data=data, ins=ins)

    # Load metadata
    ins = ['<h1>Redirecting...</h1>', 'href="%s"' % url_redirect_to_clusterreports]
    req(app, client, view='clusterreports', kws=dict(node=1), ins=ins)


# Backend cross-node report aggregation (clusterreports.json) for monitoring scripts / external tools.
# Node 1 (local Scrapyd) resolves the demo log; node 2 (fake domain) is unreachable -> log_missing.
def test_cluster_reports_json(app, client):
    jskeys = ['status', 'project', 'spider', 'job', 'selected_nodes', 'summary', 'aggregated', 'nodes']

    # Select nodes via checkbox-style POST, the same convention as the servers page.
    kws = dict(node=1, project=cst.PROJECT, spider=cst.SPIDER, job=cst.DEMO_JOBID)
    data = {'1': 'on', '2': 'on'}
    __, js = req(app, client, view='clusterreports.json', kws=kws, data=data, jskeys=jskeys,
                 jskws=dict(status=cst.OK, project=cst.PROJECT, spider=cst.SPIDER, job=cst.DEMO_JOBID))
    assert js['selected_nodes'] == [1, 2]

    # Node-level status summary tells a node missing its log apart from the rest.
    summary = js['summary']
    assert summary['selected'] == 2
    assert summary['log_missing_nodes'] == [2]
    assert summary['finished'] + summary['running'] + summary['log_missing'] == 2

    node1, node2 = js['nodes']
    # Node 2 is unreachable: the single-node degradation is preserved (report status 'error').
    assert node2['node'] == 2
    assert node2['node_status'] == 'log_missing'
    assert node2['report']['status'] == cst.ERROR
    # Node 1 returned a parseable report and contributes to the aggregate.
    assert node1['node'] == 1
    assert node1['node_status'] in ('finished', 'running')
    assert node1['report']['status'] == cst.OK
    assert node1['node'] in (summary['finished_nodes'] + summary['running_nodes'])

    # The cross-node aggregate equals the sum of the nodes that returned stats (here only node 1).
    # Asserting against node 1's own report keeps this robust to the actual crawl numbers.
    agg = js['aggregated']
    assert agg['nodes_with_stats'] == 1
    assert agg['pages'] == (node1['report'].get('pages') or 0)
    assert agg['items'] == (node1['report'].get('items') or 0)
    expected_categories = {key: (node1['report']['log_categories'][key]['count'] or 0)
                           for key in ['critical_logs', 'error_logs', 'warning_logs',
                                       'redirect_logs', 'retry_logs', 'ignore_logs']}
    assert agg['log_categories'] == expected_categories


def test_cluster_reports_json_nodes_arg_and_defaults(app, client):
    # The 'nodes' argument (comma/space separated) selects a subset, here over GET.
    kws = dict(node=1, project=cst.PROJECT, spider=cst.SPIDER, job=cst.DEMO_JOBID, nodes='1')
    __, js = req(app, client, view='clusterreports.json', kws=kws, jskws=dict(status=cst.OK))
    assert js['selected_nodes'] == [1]
    assert js['summary']['selected'] == 1
    assert len(js['nodes']) == 1 and js['nodes'][0]['node'] == 1

    # With no node specified the whole cluster is used (2 nodes in the test config).
    kws = dict(node=1, project=cst.PROJECT, spider=cst.SPIDER, job=cst.DEMO_JOBID)
    __, js = req(app, client, view='clusterreports.json', kws=kws, jskws=dict(status=cst.OK))
    assert js['selected_nodes'] == [1, 2]

    # A single-node cluster still aggregates over that one node.
    kws = dict(node=1, project=cst.PROJECT, spider=cst.SPIDER, job=cst.DEMO_JOBID)
    __, js = req_single_scrapyd(app, client, view='clusterreports.json', kws=kws, jskws=dict(status=cst.OK))
    assert js['selected_nodes'] == [1]


def test_cluster_reports_json_missing_args(app, client):
    # project / spider / job are required; the no-argument route returns a clear JSON error.
    __, js = req(app, client, view='clusterreports.json', kws=dict(node=1),
                 jskws=dict(status=cst.ERROR), ins='are all required')
    assert js['status'] == cst.ERROR
