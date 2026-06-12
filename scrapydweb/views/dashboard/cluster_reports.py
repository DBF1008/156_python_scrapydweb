# coding: utf-8
from flask import redirect, render_template, url_for

from ..baseview import BaseView


class ClusterReportsView(BaseView):
    """Render Scrapy log reports for one job across one or more nodes.

    All navigation state comes from the current request only, which is the single,
    consistent source of truth:
      * current job          -> URL path params (project / spider / job)
      * current nodes        -> POSTed checkboxes via get_selected_nodes()
      * jump-back source      -> the 'Select nodes' / 'Select a job' links, derived
                                 from the two above

    No process-global state is kept, so switching jobs or nodes never leaks a previous
    selection, and the behaviour is identical before and after a restart.
    """

    def __init__(self):
        super(ClusterReportsView, self).__init__()

        # Normalise the route's "absent" sentinel (None) to '' so that the first-entry landing
        # page (no job) still builds url_for('log', ...) and renders '0 Reports of ////'.
        # '' and None are both falsy, so the all()/any() checks below are unaffected.
        self.project = self.view_args['project'] or ''
        self.spider = self.view_args['spider'] or ''
        self.job = self.view_args['job'] or ''
        # [] on GET; populated only on the POST coming from the Servers 'Get Reports' form
        self.selected_nodes = self.get_selected_nodes()

        self.template = 'scrapydweb/cluster_reports.html'

    def dispatch_request(self, **kwargs):
        # A job is fully addressed but no nodes were selected (the Reports button on the
        # Jobs page, or a refresh of a job URL): send the user to the Servers page to pick
        # the nodes for THIS job.
        if all([self.project, self.spider, self.job]) and not self.selected_nodes:
            return redirect(url_for('servers', node=self.node, opt='getreports', project=self.project,
                                    spider=self.spider, version_job=self.job))

        # First entry (the Reports menu, no job): show the landing page that links to the
        # Jobs page so the user can pick a job.
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
