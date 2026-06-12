# coding: utf-8
from flask import get_flashed_messages, redirect, render_template, request, url_for

from ..baseview import BaseView


metadata = dict(
    project='',
    spider='',
    job='',
    selected_nodes=[]
)

# Log category keys as returned in the simplified report's log_categories dict
_LOG_CATEGORY_KEYS = ['critical_logs', 'error_logs', 'warning_logs',
                      'redirect_logs', 'retry_logs', 'ignore_logs']
# Mapping from log_categories key to summary aggregation key
_LOG_CATEGORY_SUMMARY_MAP = {
    'critical_logs': 'total_log_critical',
    'error_logs': 'total_log_error',
    'warning_logs': 'total_log_warning',
    'redirect_logs': 'total_log_redirect',
    'retry_logs': 'total_log_retry',
    'ignore_logs': 'total_log_ignore',
}


class ClusterReportApiView(BaseView):
    """Backend aggregation endpoint for cross-node report summaries.

    Accepts project/spider/job and a set of selected nodes, fetches the
    individual report from each node via the existing ``/log/report/``
    endpoint, classifies every node as *finished*, *running*, or *error*,
    and returns a single JSON payload with per-node details plus aggregated
    totals.

    NOTE: Calls to each node are sequential (via ``get_response_from_view``),
    which mirrors the pattern used by ``NodeReportsView`` and other views.
    For large node counts this may be slow — a future enhancement could
    parallelise the calls with threading.
    """

    def __init__(self):
        super(ClusterReportApiView, self).__init__()
        self.project = self.view_args['project']
        self.spider = self.view_args['spider']
        self.job = self.view_args['job']

    # ------------------------------------------------------------------
    # Input helpers
    # ------------------------------------------------------------------
    def _parse_nodes_param(self, nodes_str):
        """Parse a comma-separated string of node indices.

        Returns ``(valid_nodes, invalid_nodes)`` where each is a list of
        ints.  ``valid_nodes`` only contains indices within the legal
        range ``[1, SCRAPYD_SERVERS_AMOUNT]`` and is deduplicated while
        preserving first-seen order.
        """
        nodes = []
        invalid = []
        for part in nodes_str.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                invalid.append(part)
                continue
            if 1 <= n <= self.SCRAPYD_SERVERS_AMOUNT:
                if n not in nodes:
                    nodes.append(n)
            else:
                invalid.append(part)
        return nodes, invalid

    def _resolve_selected_nodes(self):
        """Determine the selected node list from the request.

        Supports two input formats (the ``nodes`` param takes priority):

        1. ``nodes=1,2,3`` — query-string or form field (API-friendly)
        2. ``{1: 'on', 2: 'on'}`` — classic checkbox form data
           (via :meth:`get_selected_nodes`)

        Returns ``(selected_nodes, error_response_or_None)``.
        """
        # 1. Explicit ``nodes`` param (query-string or form)
        nodes_str = (request.values.get('nodes')
                     or (request.json or {}).get('nodes'))
        if nodes_str:
            if isinstance(nodes_str, list):
                # JSON body with a list: {"nodes": [1, 2]}
                nodes_str = ','.join(str(n) for n in nodes_str)
            selected_nodes, invalid = self._parse_nodes_param(str(nodes_str))
            if invalid:
                return None, self._error_response(
                    400,
                    "Invalid node index(es): %s. Valid range: 1-%d"
                    % (', '.join(str(i) for i in invalid),
                       self.SCRAPYD_SERVERS_AMOUNT))
            if not selected_nodes:
                return None, self._error_response(
                    400, "No valid nodes selected")
            return selected_nodes, None

        # 2. Classic checkbox form data
        selected_nodes = self.get_selected_nodes()
        if not selected_nodes:
            return None, self._error_response(
                400,
                "No nodes selected. Provide node indices via the 'nodes' "
                "parameter (e.g. nodes=1,2,3) or as form checkboxes.")
        return selected_nodes, None

    def _error_response(self, status_code, message):
        """Build a JSON error response tuple ``(response, status_code)``."""
        body = dict(
            status=self.ERROR,
            message=message,
            status_code=status_code,
            when=self.get_now_string(True),
        )
        return self.json_dumps(body, as_response=True), status_code

    # ------------------------------------------------------------------
    # Per-node fetching & classification
    # ------------------------------------------------------------------
    def _fetch_node_report(self, node_idx):
        """Fetch the report for a single node and return a result dict.

        Returns a dict with keys:
        ``node``, ``node_server``, ``node_state``, ``report``,
        and optionally ``error_message``.
        """
        node_server = self.SCRAPYD_SERVERS[node_idx - 1]
        url = url_for('log', node=node_idx, opt='report',
                      project=self.project, spider=self.spider,
                      job=self.job)
        js = self.get_response_from_view(url, as_json=True)
        # Clear any flash messages accumulated from the internal LogView
        # call so they don't leak into our API response.
        get_flashed_messages()

        if not isinstance(js, dict):
            return dict(
                node=node_idx,
                node_server=node_server,
                node_state='error',
                report=None,
                error_message='Unexpected response type from report endpoint',
            )

        status = js.get('status', '')
        if status != self.OK:
            # Covers: connection failure (status_code=-1), missing log
            # (status_code=404), LogParser version mismatch, etc.
            return dict(
                node=node_idx,
                node_server=node_server,
                node_state='error',
                report=None,
                error_message=js.get('message', status),
            )

        # status == 'ok' — classify as finished or running
        shutdown_reason = js.get('shutdown_reason', self.NA)
        finish_reason = js.get('finish_reason', self.NA)
        if shutdown_reason != self.NA or finish_reason != self.NA:
            node_state = 'finished'
        else:
            node_state = 'running'

        return dict(
            node=node_idx,
            node_server=node_server,
            node_state=node_state,
            report=js,
        )

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------
    @staticmethod
    def _build_summary(node_results):
        """Aggregate totals across all node results."""
        summary = dict(
            total_pages=0,
            total_items=0,
            total_log_critical=0,
            total_log_error=0,
            total_log_warning=0,
            total_log_redirect=0,
            total_log_retry=0,
            total_log_ignore=0,
            finished_amount=0,
            running_amount=0,
            error_amount=0,
        )
        for r in node_results:
            state = r['node_state']
            if state == 'finished':
                summary['finished_amount'] += 1
            elif state == 'running':
                summary['running_amount'] += 1
            else:
                summary['error_amount'] += 1

            if state in ('finished', 'running') and r.get('report'):
                report = r['report']
                summary['total_pages'] += report.get('pages') or 0
                summary['total_items'] += report.get('items') or 0

                log_categories = report.get('log_categories', {})
                for cat_key, summary_key in _LOG_CATEGORY_SUMMARY_MAP.items():
                    cat_data = log_categories.get(cat_key, {})
                    if isinstance(cat_data, dict):
                        summary[summary_key] += cat_data.get('count', 0)
                    else:
                        summary[summary_key] += cat_data or 0
        return summary

    # ------------------------------------------------------------------
    # Main dispatch
    # ------------------------------------------------------------------
    def dispatch_request(self, **kwargs):
        if not all([self.project, self.spider, self.job]):
            return self._error_response(
                400, "project, spider, and job are all required")

        selected_nodes, err = self._resolve_selected_nodes()
        if err is not None:
            return err

        node_results = [self._fetch_node_report(n) for n in selected_nodes]
        summary = self._build_summary(node_results)

        result = dict(
            status=self.OK,
            project=self.project,
            spider=self.spider,
            job=self.job,
            selected_nodes=selected_nodes,
            summary=summary,
            node_results=node_results,
            url_cluster_reports=url_for(
                'clusterreports', node=self.node,
                project=self.project, spider=self.spider, job=self.job),
            when=self.get_now_string(True),
        )
        return self.json_dumps(result, as_response=True)


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
