# coding: utf-8
from collections import OrderedDict
import re

from flask import redirect, render_template, request, url_for

from ..baseview import BaseView


metadata = dict(
    project='',
    spider='',
    job='',
    selected_nodes=[]
)

# Keys of the per-node report produced by LogView(opt='report'), see scrapydweb/views/files/log.py
# Used to sum up numeric counters across nodes in ClusterReportsJsonView.
REPORT_LOG_CATEGORY_KEYS = ['critical_logs', 'error_logs', 'warning_logs',
                            'redirect_logs', 'retry_logs', 'ignore_logs']

# Node-level status values reported by ClusterReportsJsonView.
NODE_STATUS_FINISHED = 'finished'      # node returned a report and the job has stopped
NODE_STATUS_RUNNING = 'running'        # node returned a report and the job is still running
NODE_STATUS_LOG_MISSING = 'log_missing'  # node has no parseable log (missing log or unreachable node)


class ClusterReportsView(BaseView):

    def __init__(self):
        super(ClusterReportsView, self).__init__()

        self.project = self.view_args['project'] or metadata['project']
        self.spider = self.view_args['spider'] or metadata['spider']
        self.job = self.view_args['job'] or metadata['job']
        self.selected_nodes = self.get_selected_nodes() or metadata['selected_nodes']
        metadata['project'] = self.project
        metadata['spider'] = self.spider
        metadata['job'] = self.job
        metadata['selected_nodes'] = self.selected_nodes

        self.template = 'scrapydweb/cluster_reports.html'

    def dispatch_request(self, **kwargs):
        if all([self.project, self.spider, self.job]):
            # Click reports memu for the second time
            if not any([self.view_args['project'], self.view_args['spider'], self.view_args['job']]):
                return redirect(url_for('clusterreports', node=self.node, project=self.project,
                                        spider=self.spider, job=self.job))
            # Click reports button on the Jobs page after reboot
            if not self.selected_nodes:
                return redirect(url_for('servers', node=self.node, opt='getreports', project=self.project,
                                        spider=self.spider, version_job=self.job))

        # Click reports memu for the first time
        if not any([self.project, self.spider, self.job]):
            url_servers = ''
        else:
            url_servers = url_for('servers', node=self.node, opt='getreports', project=self.project,
                                  spider=self.spider, version_job=self.job)

        kwargs = dict(
            node=self.node,
            project=self.project,
            spider=self.spider,
            job=self.job,
            selected_nodes=self.selected_nodes,
            url_report=url_for('log', node=self.node, opt='report', project=self.project,
                               spider=self.spider, job=self.job),
            url_servers=url_servers,
            url_jobs=url_for('jobs', node=self.node),
            # url_nodereports=url_for('nodereports', node=self.node),
        )
        return render_template(self.template, **kwargs)


class ClusterReportsJsonView(BaseView):
    """Backend cross-node report aggregation for monitoring scripts and external tools.

    The ClusterReportsView page lets the browser fetch each node's report one by one and
    aggregates them client side, so external callers cannot get a single aggregated result
    and cannot tell a node that is missing its log apart from a node whose job is still running.

    This view does the fan-out on the server side instead: given a project / spider / job and a
    set of selected nodes, it returns a single JSON document containing
      * ``aggregated``: the cross-node aggregated report (summed pages, items and log counters), and
      * ``summary`` + per-node ``nodes``: a node-level status summary that tells
        finished / running / log-missing nodes apart.

    It reuses LogView(opt='report') for every node, so the existing single-node report interface
    and its log-missing degradation behaviour (a node with no parseable log yields status 'error')
    are preserved untouched.

    Selecting nodes (first match wins):
      1. an explicit ``nodes`` argument, comma/space separated, e.g. ``?nodes=1,2,3`` (GET or POST);
      2. checkbox-style form fields ``{'1': 'on', '2': 'on'}`` (same convention as the servers page);
      3. otherwise every node in the cluster.

    project / spider / job may be supplied in the URL path or, for convenience, as query/form args.
    """

    def __init__(self):
        super(ClusterReportsJsonView, self).__init__()

        self.project = self.view_args['project'] or request.values.get('project', '')
        self.spider = self.view_args['spider'] or request.values.get('spider', '')
        self.job = self.view_args['job'] or request.values.get('job', '')
        self.selected_nodes = self.get_selected_report_nodes()

    def get_selected_report_nodes(self):
        # 1) explicit comma/space separated list, convenient for monitoring scripts and external tools
        nodes_arg = request.values.get('nodes', '')
        if nodes_arg:
            selected_nodes = []
            for chunk in re.split(r'[,\s]+', nodes_arg.strip()):
                if not chunk:
                    continue
                try:
                    node = int(chunk)
                except ValueError:
                    continue
                if 1 <= node <= self.SCRAPYD_SERVERS_AMOUNT and node not in selected_nodes:
                    selected_nodes.append(node)
            return selected_nodes
        # 2) checkbox-style form fields posted from a page
        selected_nodes = self.get_selected_nodes()
        if selected_nodes:
            return selected_nodes
        # 3) default to the whole cluster
        return list(range(1, self.SCRAPYD_SERVERS_AMOUNT + 1))

    def dispatch_request(self, **kwargs):
        if not all([self.project, self.spider, self.job]):
            return self.error_response("The 'project', 'spider' and 'job' arguments are all required")
        if not self.selected_nodes:
            return self.error_response(
                "No valid node selected, expected node index between 1 and %s" % self.SCRAPYD_SERVERS_AMOUNT)

        nodes = []
        for node in self.selected_nodes:
            report = self.request_node_report(node)
            nodes.append(dict(node=node, node_status=self.classify_report(report), report=report))

        result = dict(
            status=self.OK,
            project=self.project,
            spider=self.spider,
            job=self.job,
            selected_nodes=self.selected_nodes,
            summary=self.build_summary(nodes),
            aggregated=self.aggregate_reports(nodes),
            nodes=nodes,
        )
        return self.json_dumps(result, as_response=True)

    def error_response(self, message):
        result = dict(
            status=self.ERROR,
            message=message,
            project=self.project,
            spider=self.spider,
            job=self.job,
            selected_nodes=self.selected_nodes,
        )
        return self.json_dumps(result, as_response=True), 400

    def request_node_report(self, node):
        # Reuse the single-node report endpoint so its log-missing degradation behaviour is preserved.
        url = url_for('log', node=node, opt='report',
                      project=self.project, spider=self.spider, job=self.job)
        report = self.get_response_from_view(url, as_json=True)
        if not isinstance(report, dict):
            report = dict(status=self.ERROR, message='Unexpected report response from node %s' % node)
        return report

    def classify_report(self, report):
        # A node with no parseable log (missing log or unreachable node) yields status 'error'.
        if report.get('status') != self.OK:
            return NODE_STATUS_LOG_MISSING
        # Mirror the page logic: a job is finished once it has a finish_reason or a shutdown_reason.
        if (report.get('finish_reason', self.NA) != self.NA
                or report.get('shutdown_reason', self.NA) != self.NA):
            return NODE_STATUS_FINISHED
        return NODE_STATUS_RUNNING

    @staticmethod
    def build_summary(nodes):
        finished_nodes = [n['node'] for n in nodes if n['node_status'] == NODE_STATUS_FINISHED]
        running_nodes = [n['node'] for n in nodes if n['node_status'] == NODE_STATUS_RUNNING]
        log_missing_nodes = [n['node'] for n in nodes if n['node_status'] == NODE_STATUS_LOG_MISSING]
        return dict(
            selected=len(nodes),
            finished=len(finished_nodes),
            running=len(running_nodes),
            log_missing=len(log_missing_nodes),
            finished_nodes=finished_nodes,
            running_nodes=running_nodes,
            log_missing_nodes=log_missing_nodes,
        )

    @staticmethod
    def aggregate_reports(nodes):
        nodes_with_stats = 0
        pages = 0
        items = 0
        log_categories = OrderedDict((key, 0) for key in REPORT_LOG_CATEGORY_KEYS)
        for n in nodes:
            # Only nodes that returned a parseable report contribute to the aggregated totals.
            if n['node_status'] == NODE_STATUS_LOG_MISSING:
                continue
            report = n['report']
            nodes_with_stats += 1
            # pages and items may be None when reported by LogParser.
            pages += report.get('pages') or 0
            items += report.get('items') or 0
            categories = report.get('log_categories') or {}
            for key in REPORT_LOG_CATEGORY_KEYS:
                try:
                    log_categories[key] += categories[key]['count'] or 0
                except (KeyError, TypeError):
                    pass
        return dict(
            nodes_with_stats=nodes_with_stats,
            pages=pages,
            items=items,
            log_categories=log_categories,
        )
