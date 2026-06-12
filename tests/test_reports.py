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


# ClusterReportsView derives all of its navigation state (current job, current nodes,
# jump-back source) from the current request only -- there is no process-global module
# state. The cases below assert that single, consistent source of truth and, crucially,
# that nothing leaks between requests/jobs (which used to cause the "snap back to the last
# selection" bug and the selected-nodes / current-job mismatch after a restart).
def test_cluster_reports(app, client):
    with app.test_request_context():
        url_jobs = url_for('jobs', node=1)
        url_servers = url_for('servers', node=2, opt='getreports', project=cst.PROJECT,
                              spider=cst.SPIDER, version_job=cst.JOBID)
        url_report = url_for('log', node=2, opt='report', project=cst.PROJECT,
                             spider=cst.SPIDER, job=cst.JOBID)

    # 1) First entry (Reports menu, no job): the empty landing page that links to the Jobs
    #    page. No "Select nodes" button because there is no job to select nodes for yet.
    ins = ['0 Reports of ////', '>Select a job</el-button>', url_jobs, 'selected_nodes: [],']
    nos = ['>Select nodes</el-button>', '<h1>Redirecting...</h1>']
    req(app, client, view='clusterreports', kws=dict(node=1), ins=ins, nos=nos)

    # 2) Re-select nodes round-trip: the Servers "Get Reports" form POSTs the selected node
    #    checkboxes to clusterreports/<project>/<spider>/<job>; the reports render for exactly
    #    those nodes, with the "Select nodes" jump-back link available.
    data = {'1': 'on', '2': 'on'}
    ins = [
        '%s Reports of /%s/%s/%s/' % (len(data), cst.PROJECT, cst.SPIDER, cst.JOBID),
        'selected_nodes: [1, 2],',
        '>Select nodes</el-button>',
        '>Select a job</el-button>',
        url_servers,
        url_report,
    ]
    kws = dict(node=2, project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID)
    req(app, client, view='clusterreports', kws=kws, data=data, ins=ins)

    # 3) Regression: clicking the Reports menu again (no params) must NOT snap back to the
    #    job/nodes viewed in step 2. It is stateless, so it shows the same empty landing page
    #    -- never a redirect to the previous selection. (This replaces the old "Load metadata"
    #    assertion, which encoded the bug.)
    ins = ['0 Reports of ////', '>Select a job</el-button>', 'selected_nodes: [],']
    nos = ['>Select nodes</el-button>', '<h1>Redirecting...</h1>', cst.JOBID]
    req(app, client, view='clusterreports', kws=dict(node=1), ins=ins, nos=nos)


# Regression for the reported bug: switching to a different job (or entering one with no node
# selection) must route through node selection for THAT job and never reuse a previous job's
# nodes. Behaviour is identical whether or not another report was viewed first, which is also
# what makes it stable across a restart (there is no stored state to lose or mismatch).
def test_cluster_reports_no_leak_between_jobs(app, client):
    with app.test_request_context():
        url_servers_a = url_for('servers', node=2, opt='getreports', project=cst.PROJECT,
                                spider=cst.SPIDER, version_job=cst.JOBID)
        url_servers_b = url_for('servers', node=2, opt='getreports', project=cst.FAKE_PROJECT,
                                spider=cst.FAKE_SPIDER, version_job=cst.FAKE_JOBID)

    # a) Reports button on the Jobs page (a fully-addressed job, GET, no nodes) deterministically
    #    redirects to the Servers page to pick the nodes for that job.
    req(app, client, view='clusterreports',
        kws=dict(node=2, project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID),
        location=url_servers_a,
        ins=['<h1>Redirecting...</h1>', 'href="%s"' % url_servers_a])

    # b) View job A on some nodes (in the old code this polluted the shared module dict)...
    req(app, client, view='clusterreports',
        kws=dict(node=2, project=cst.PROJECT, spider=cst.SPIDER, job=cst.JOBID),
        data={'1': 'on', '2': 'on'},
        ins=['2 Reports of /%s/%s/%s/' % (cst.PROJECT, cst.SPIDER, cst.JOBID), 'selected_nodes: [1, 2],'])

    # ...then open a DIFFERENT job B with no node selection: it must redirect to node selection
    #    for job B, NOT render job B with job A's stale nodes (cst.JOBID must be absent).
    req(app, client, view='clusterreports',
        kws=dict(node=2, project=cst.FAKE_PROJECT, spider=cst.FAKE_SPIDER, job=cst.FAKE_JOBID),
        location=url_servers_b,
        ins=['<h1>Redirecting...</h1>', 'href="%s"' % url_servers_b, cst.FAKE_JOBID],
        nos=[cst.JOBID])
