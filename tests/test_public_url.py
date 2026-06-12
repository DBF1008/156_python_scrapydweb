# coding: utf-8
"""Regression tests for SCRAPYD_SERVERS_PUBLIC_URLS (reverse proxy) support.

Verifies that all browser-facing URLs correctly use the configured public URL
when SCRAPYD_SERVERS_PUBLIC_URLS is set, and fall back to internal direct
URLs when it is not configured.
"""
import re
from unittest.mock import MagicMock, patch

import pytest

from scrapydweb import create_app

PUBLIC_URL = 'https://proxy.example.com'
SCRAPYD_SERVER = '10.0.0.1:6800'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_app(public_urls):
    """Create a test Flask app with the given SCRAPYD_SERVERS_PUBLIC_URLS."""
    config = dict(
        TESTING=True,
        SECRET_KEY='dev',
        DEFAULT_SETTINGS_PY_PATH='',
        SCRAPYDWEB_SETTINGS_PY_PATH='',
        MAIN_PID=0,
        LOGPARSER_PID=0,
        POLL_PID=0,
        SCRAPYD_SERVERS=[SCRAPYD_SERVER],
        _SCRAPYD_SERVERS=[SCRAPYD_SERVER],
        SCRAPYD_SERVERS_AUTHS=[None],
        SCRAPYD_SERVERS_GROUPS=[''],
        SCRAPYD_SERVERS_PUBLIC_URLS=public_urls,
        LOCAL_SCRAPYD_SERVER=SCRAPYD_SERVER,
        LOCAL_SCRAPYD_LOGS_DIR='/tmp/fake-logs',
        ENABLE_LOGPARSER=False,
        VERBOSE=False,
        SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
        SQLALCHEMY_BINDS={'jobs': 'sqlite:///:memory:'},
    )
    app = create_app(config)

    @app.context_processor
    def inject_variable():
        servers = app.config.get('SCRAPYD_SERVERS', [])
        return dict(
            SCRAPYD_SERVERS=servers,
            SCRAPYD_SERVERS_AMOUNT=len(servers),
            SCRAPYD_SERVERS_GROUPS=app.config.get('SCRAPYD_SERVERS_GROUPS', ['']),
            SCRAPYD_SERVERS_AUTHS=app.config.get('SCRAPYD_SERVERS_AUTHS', [None]),
            SCRAPYD_SERVERS_PUBLIC_URLS=(app.config.get('SCRAPYD_SERVERS_PUBLIC_URLS', None)
                                         or [''] * len(servers)),
            DAEMONSTATUS_REFRESH_INTERVAL=10,
            ENABLE_AUTH=False,
            SHOW_SCRAPYD_ITEMS=True,
        )

    return app


@pytest.fixture
def app_with_public_url():
    return _make_app([PUBLIC_URL])


@pytest.fixture
def app_without_public_url():
    return _make_app([''])


@pytest.fixture
def client_public(app_with_public_url):
    return app_with_public_url.test_client()


@pytest.fixture
def client_direct(app_without_public_url):
    return app_without_public_url.test_client()


# Minimal Scrapyd HTML responses for view rendering.
# NOTE: The directory listing HTML must match the DIRECTORY_PATTERN regex in vars.py
# with specific newlines and indentation for the row cells.
JOBS_HTML = '''\
<html>
<body>
<h1>Jobs</h1>
<table>
<tr><th>Project</th><th>Spider</th><th>Job</th><th>PID</th>\
<th>Start</th><th>Runtime</th><th>Finish</th><th>Log</th><th>Items</th></tr>
</table>
</body>
</html>
'''

LOGS_DIR_HTML = (
    '<html><body><h1>Directory listing for /logs/demo/test/</h1>\n'
    '<table>\n'
    '<tr class="odd">\n'
    '    <td><a href="job1.log">job1.log</a></td>\n'
    '    <td>10K</td>\n'
    '    <td>2024-01-01 00:00</td>\n'
    '    <td>text/plain</td>\n'
    '    <td></td>\n'
    '</tr>\n'
    '</table>\n'
    '</body></html>'
)

ITEMS_DIR_HTML = (
    '<html><body><h1>Directory listing for /items/demo/test/</h1>\n'
    '<table>\n'
    '<tr class="odd">\n'
    '    <td><a href="job1.jl">job1.jl</a></td>\n'
    '    <td>10K</td>\n'
    '    <td>2024-01-01 00:00</td>\n'
    '    <td>text/plain</td>\n'
    '    <td></td>\n'
    '</tr>\n'
    '</table>\n'
    '</body></html>'
)


def _mock_response(status_code=200, text=''):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.encoding = 'utf-8'
    return resp


# ---------------------------------------------------------------------------
# JobsView public URL tests
# ---------------------------------------------------------------------------

class TestJobsViewPublicURL:
    """Verify JobsView generates correct public URLs."""

    def test_public_url_attribute(self, app_with_public_url):
        with app_with_public_url.test_request_context('/1/jobs/'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == PUBLIC_URL
            assert view.public_url == PUBLIC_URL + '/jobs'
            assert view.url == 'http://%s/jobs' % SCRAPYD_SERVER

    def test_set_kwargs_url_for_display(self, app_with_public_url):
        with app_with_public_url.test_request_context('/1/jobs/?style=classic'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            view.pending_jobs = []
            view.running_jobs = []
            view.finished_jobs = []
            view.set_kwargs()
            assert view.kwargs['url'] == PUBLIC_URL + '/jobs'

    def test_set_kwargs_url_liststats_source(self, app_with_public_url):
        with app_with_public_url.test_request_context('/1/jobs/'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            view.pending_jobs = []
            view.running_jobs = []
            view.finished_jobs = []
            view.set_kwargs()
            assert view.kwargs['url_liststats_source'] == PUBLIC_URL + '/logs/stats.json'
            assert SCRAPYD_SERVER not in view.kwargs['url_liststats_source']

    @patch('scrapydweb.views.baseview.session')
    def test_full_classic_render(self, mock_session, app_with_public_url, client_public):
        mock_session.get.return_value = _mock_response(200, JOBS_HTML)
        response = client_public.get('/1/jobs/?style=classic')
        html = response.get_data(as_text=True)
        # The heading link should use public URL
        assert 'href="%s/jobs"' % PUBLIC_URL in html
        # The LogParser info text should use public URL
        assert PUBLIC_URL + '/logs/stats.json' in html
        # The internal URL should NOT appear as a browser-facing link
        assert 'http://%s/jobs' % SCRAPYD_SERVER not in html


class TestJobsViewDirectURL:
    """Verify JobsView falls back to internal URL when no public URL is configured."""

    def test_no_public_url_attribute(self, app_without_public_url):
        with app_without_public_url.test_request_context('/1/jobs/'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == ''
            assert view.public_url == ''

    def test_set_kwargs_uses_internal_url(self, app_without_public_url):
        with app_without_public_url.test_request_context('/1/jobs/?style=classic'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            view.pending_jobs = []
            view.running_jobs = []
            view.finished_jobs = []
            view.set_kwargs()
            expected = 'http://%s/jobs' % SCRAPYD_SERVER
            assert view.kwargs['url'] == expected
            assert view.kwargs['url_liststats_source'] == expected.replace('/jobs', '/logs/stats.json')

    @patch('scrapydweb.views.baseview.session')
    def test_full_classic_render(self, mock_session, app_without_public_url, client_direct):
        mock_session.get.return_value = _mock_response(200, JOBS_HTML)
        response = client_direct.get('/1/jobs/?style=classic')
        html = response.get_data(as_text=True)
        expected = 'http://%s/jobs' % SCRAPYD_SERVER
        assert expected in html


# ---------------------------------------------------------------------------
# LogsView public URL tests
# ---------------------------------------------------------------------------

class TestLogsViewPublicURL:
    """Verify LogsView generates correct public URLs."""

    def test_public_url_attribute(self, app_with_public_url):
        with app_with_public_url.test_request_context('/1/logs/demo/test/'):
            from scrapydweb.views.files.logs import LogsView
            view = LogsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == PUBLIC_URL
            assert view.public_url == PUBLIC_URL + '/logs/demo/test/'
            assert view.url == 'http://%s/logs/demo/test/' % SCRAPYD_SERVER

    def test_public_url_root_logs(self, app_with_public_url):
        with app_with_public_url.test_request_context('/1/logs/'):
            from scrapydweb.views.files.logs import LogsView
            view = LogsView()
            assert view.public_url == PUBLIC_URL + '/logs/'

    @patch('scrapydweb.views.baseview.session')
    def test_full_render(self, mock_session, app_with_public_url, client_public):
        mock_session.get.return_value = _mock_response(200, LOGS_DIR_HTML)
        response = client_public.get('/1/logs/demo/test/')
        html = response.get_data(as_text=True)
        # The heading link should use public URL
        assert 'href="%s/logs/demo/test/"' % PUBLIC_URL in html
        # File links should use public URL
        assert PUBLIC_URL + '/logs/demo/test/job1.log' in html
        # Internal URL should not appear in href attributes
        assert 'http://%s' % SCRAPYD_SERVER not in html


class TestLogsViewDirectURL:
    """Verify LogsView falls back to internal URL when no public URL is configured."""

    def test_no_public_url_attribute(self, app_without_public_url):
        with app_without_public_url.test_request_context('/1/logs/demo/test/'):
            from scrapydweb.views.files.logs import LogsView
            view = LogsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == ''
            assert view.public_url == ''

    @patch('scrapydweb.views.baseview.session')
    def test_full_render(self, mock_session, app_without_public_url, client_direct):
        mock_session.get.return_value = _mock_response(200, LOGS_DIR_HTML)
        response = client_direct.get('/1/logs/demo/test/')
        html = response.get_data(as_text=True)
        expected = 'http://%s/logs/demo/test/' % SCRAPYD_SERVER
        assert expected in html


# ---------------------------------------------------------------------------
# ItemsView public URL tests
# ---------------------------------------------------------------------------

class TestItemsViewPublicURL:
    """Verify ItemsView generates correct public URLs."""

    def test_public_url_attribute(self, app_with_public_url):
        with app_with_public_url.test_request_context('/1/items/demo/test/'):
            from scrapydweb.views.files.items import ItemsView
            view = ItemsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == PUBLIC_URL
            assert view.public_url == PUBLIC_URL + '/items/demo/test/'
            assert view.url == 'http://%s/items/demo/test/' % SCRAPYD_SERVER

    @patch('scrapydweb.views.baseview.session')
    def test_full_render(self, mock_session, app_with_public_url, client_public):
        mock_session.get.return_value = _mock_response(200, ITEMS_DIR_HTML)
        response = client_public.get('/1/items/demo/test/')
        html = response.get_data(as_text=True)
        # The heading link should use public URL
        assert 'href="%s/items/demo/test/"' % PUBLIC_URL in html
        # File links should use public URL
        assert PUBLIC_URL + '/items/demo/test/job1.jl' in html
        # Internal URL should not appear in href attributes
        assert 'http://%s' % SCRAPYD_SERVER not in html


class TestItemsViewDirectURL:
    """Verify ItemsView falls back to internal URL when no public URL is configured."""

    def test_no_public_url_attribute(self, app_without_public_url):
        with app_without_public_url.test_request_context('/1/items/demo/test/'):
            from scrapydweb.views.files.items import ItemsView
            view = ItemsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == ''
            assert view.public_url == ''

    @patch('scrapydweb.views.baseview.session')
    def test_full_render(self, mock_session, app_without_public_url, client_direct):
        mock_session.get.return_value = _mock_response(200, ITEMS_DIR_HTML)
        response = client_direct.get('/1/items/demo/test/')
        html = response.get_data(as_text=True)
        expected = 'http://%s/items/demo/test/' % SCRAPYD_SERVER
        assert expected in html


# ---------------------------------------------------------------------------
# LogView url_source construction tests (already worked; regression guard)
# ---------------------------------------------------------------------------

class TestLogViewPublicURL:
    """Verify LogView constructs correct public URL for url_source.

    LogView.__init__ requires the APScheduler scheduler to be running,
    so we test the regex substitution logic that update_kwargs() uses
    without instantiating the full view.
    """

    @staticmethod
    def _compute_url_source(url, scrapyd_server_public_url):
        """Replicate the url_source logic from LogView.update_kwargs()."""
        if scrapyd_server_public_url:
            return re.sub(
                r'^http.*?/logs/',
                scrapyd_server_public_url + '/logs/',
                url
            )
        return url

    def test_url_source_with_public_url(self):
        url = 'http://%s/logs/demo/test/job1.log' % SCRAPYD_SERVER
        url_source = self._compute_url_source(url, PUBLIC_URL)
        assert url_source == PUBLIC_URL + '/logs/demo/test/job1.log'
        assert SCRAPYD_SERVER not in url_source

    def test_url_source_without_public_url(self):
        url = 'http://%s/logs/demo/test/job1.log' % SCRAPYD_SERVER
        url_source = self._compute_url_source(url, '')
        assert url_source == url

    def test_url_source_with_extension(self):
        """url may include a file extension appended after initial construction."""
        url = 'http://%s/logs/demo/test/job1' % SCRAPYD_SERVER
        url += '.log'  # appended in LogView.dispatch_request()
        url_source = self._compute_url_source(url, PUBLIC_URL)
        assert url_source == PUBLIC_URL + '/logs/demo/test/job1.log'

    def test_url_source_with_path_prefix(self):
        prefixed_url = 'https://proxy.example.com/scrapyd'
        url = 'http://%s/logs/demo/test/job1.log' % SCRAPYD_SERVER
        url_source = self._compute_url_source(url, prefixed_url)
        assert url_source == prefixed_url + '/logs/demo/test/job1.log'


# ---------------------------------------------------------------------------
# Multi-node public URL tests
# ---------------------------------------------------------------------------

class TestMultiNodePublicURL:
    """Verify per-node public URL configuration works correctly."""

    def test_per_node_public_url(self):
        """First node has public URL, second node does not."""
        config = dict(
            TESTING=True,
            SECRET_KEY='dev',
            DEFAULT_SETTINGS_PY_PATH='',
            SCRAPYDWEB_SETTINGS_PY_PATH='',
            MAIN_PID=0,
            LOGPARSER_PID=0,
            POLL_PID=0,
            SCRAPYD_SERVERS=[SCRAPYD_SERVER, '10.0.0.2:6800'],
            _SCRAPYD_SERVERS=[SCRAPYD_SERVER, '10.0.0.2:6800'],
            SCRAPYD_SERVERS_AUTHS=[None, None],
            SCRAPYD_SERVERS_GROUPS=['', ''],
            SCRAPYD_SERVERS_PUBLIC_URLS=[PUBLIC_URL, ''],
            LOCAL_SCRAPYD_SERVER=SCRAPYD_SERVER,
            LOCAL_SCRAPYD_LOGS_DIR='/tmp/fake-logs',
            ENABLE_LOGPARSER=False,
            VERBOSE=False,
            SQLALCHEMY_DATABASE_URI='sqlite:///:memory:',
            SQLALCHEMY_BINDS={'jobs': 'sqlite:///:memory:'},
        )
        app = create_app(config)

        @app.context_processor
        def inject_variable():
            servers = app.config['SCRAPYD_SERVERS']
            return dict(
                SCRAPYD_SERVERS=servers,
                SCRAPYD_SERVERS_AMOUNT=len(servers),
                SCRAPYD_SERVERS_GROUPS=app.config.get('SCRAPYD_SERVERS_GROUPS', ['', '']),
                SCRAPYD_SERVERS_AUTHS=app.config.get('SCRAPYD_SERVERS_AUTHS', [None, None]),
                SCRAPYD_SERVERS_PUBLIC_URLS=app.config.get('SCRAPYD_SERVERS_PUBLIC_URLS',
                                                           ['', '']),
                DAEMONSTATUS_REFRESH_INTERVAL=10,
                ENABLE_AUTH=False,
                SHOW_SCRAPYD_ITEMS=True,
            )

        with app.test_request_context('/1/jobs/'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == PUBLIC_URL
            assert view.public_url == PUBLIC_URL + '/jobs'

        with app.test_request_context('/2/jobs/'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            assert view.SCRAPYD_SERVER_PUBLIC_URL == ''
            assert view.public_url == ''


# ---------------------------------------------------------------------------
# Public URL with path prefix tests
# ---------------------------------------------------------------------------

class TestPublicURLWithPathPrefix:
    """Verify public URLs with path prefixes work correctly."""

    def test_public_url_with_path_prefix(self):
        """Public URL may include a path prefix like /scrapyd."""
        prefixed_url = 'https://proxy.example.com/scrapyd'
        app = _make_app([prefixed_url])

        with app.test_request_context('/1/jobs/'):
            from scrapydweb.views.dashboard.jobs import JobsView
            view = JobsView()
            assert view.public_url == prefixed_url + '/jobs'

        with app.test_request_context('/1/logs/demo/test/'):
            from scrapydweb.views.files.logs import LogsView
            view = LogsView()
            assert view.public_url == prefixed_url + '/logs/demo/test/'

        with app.test_request_context('/1/items/demo/test/'):
            from scrapydweb.views.files.items import ItemsView
            view = ItemsView()
            assert view.public_url == prefixed_url + '/items/demo/test/'
