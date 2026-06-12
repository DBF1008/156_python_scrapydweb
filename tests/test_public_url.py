# coding: utf-8
"""Regression tests for the reverse proxy public URL generation chain.

When a Scrapyd node is reachable through a reverse proxy, its public address is
configured via SCRAPYD_SERVERS_PUBLIC_URLS (see issue #94). Every result link
that the browser follows to Scrapyd itself -- the "Source" / "Items" links on the
Jobs page, the directory listings on the Logs / Items pages, the "Source" link on
the Stats / Log page and the stats.json reference -- must be rewritten to that
public address so an external user can keep clicking through without hitting an
internal address. When no public URL is configured the original (direct) URL must
be returned unchanged.

These tests pin down `handle_public_url()` (the single helper the whole chain now
goes through) plus the `BaseView.make_public_url()` wrapper. They are pure string
tests: no running Scrapyd and no network are required.
"""
import pytest

# scrapydweb (and therefore the helpers under test) requires Flask; skip cleanly
# rather than error when the dependency stack is not installed.
pytest.importorskip('flask')

from six.moves.urllib.parse import urljoin

from scrapydweb.common import handle_public_url


SERVER = '127.0.0.1:6800'
PUBLIC = 'https://crawl.example.com'


# --------------------------------------------------------------------------- #
# Direct-connection semantics: no public URL configured -> URL is untouched.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('public_url', ['', None])
@pytest.mark.parametrize('url', [
    'http://127.0.0.1:6800/jobs',
    'http://127.0.0.1:6800/logs/demo/test/2018-01-01T01_01_02.log',
    'http://127.0.0.1:6800/logs/stats.json',
    'http://127.0.0.1:6800/items/demo/test/',
    'http://127.0.0.1:6800/logs/',
    'http://127.0.0.1:6800/items/',
])
def test_no_public_url_returns_url_unchanged(url, public_url):
    assert handle_public_url(url, public_url) == url


# --------------------------------------------------------------------------- #
# Reverse proxy mode: the scheme://host[:port] prefix is replaced, the rest of
# the URL (path + query string) is preserved.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('url, public_url, expected', [
    # Jobs page link and the base for per-job Source / Items links.
    ('http://127.0.0.1:6800/jobs', PUBLIC, 'https://crawl.example.com/jobs'),
    # Stats / Log page "Source" link (logfile served by Scrapyd).
    ('http://127.0.0.1:6800/logs/demo/test/2018-01-01T01_01_02.log', PUBLIC,
     'https://crawl.example.com/logs/demo/test/2018-01-01T01_01_02.log'),
    # url_liststats_source on the Jobs page.
    ('http://127.0.0.1:6800/logs/stats.json', PUBLIC,
     'https://crawl.example.com/logs/stats.json'),
    # Logs / Items directory-listing header link.
    ('http://127.0.0.1:6800/items/demo/test/', PUBLIC,
     'https://crawl.example.com/items/demo/test/'),
    ('http://127.0.0.1:6800/logs/', PUBLIC, 'https://crawl.example.com/logs/'),
    ('http://127.0.0.1:6800/items/', PUBLIC, 'https://crawl.example.com/items/'),
    # A non-default host:port is fully replaced by the public base.
    ('http://192.168.1.5:6801/logs/demo/test/a.log', PUBLIC,
     'https://crawl.example.com/logs/demo/test/a.log'),
    # An internal HTTPS source is rewritten just the same.
    ('https://127.0.0.1:6800/jobs', PUBLIC, 'https://crawl.example.com/jobs'),
    # Query strings are kept intact.
    ('http://127.0.0.1:6800/logs/stats.json?k=v', PUBLIC,
     'https://crawl.example.com/logs/stats.json?k=v'),
])
def test_public_url_replaces_host_keeps_path(url, public_url, expected):
    assert handle_public_url(url, public_url) == expected


# --------------------------------------------------------------------------- #
# A trailing slash on the configured public URL must not produce a double slash.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('public_url', [
    'https://crawl.example.com',
    'https://crawl.example.com/',
])
def test_public_url_trailing_slash_is_normalized(public_url):
    assert (handle_public_url('http://127.0.0.1:6800/logs/a.log', public_url)
            == 'https://crawl.example.com/logs/a.log')


# --------------------------------------------------------------------------- #
# A reverse proxy that mounts Scrapyd under a sub-path keeps that sub-path.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('public_url', [
    'https://crawl.example.com/scrapyd-1',
    'https://crawl.example.com/scrapyd-1/',
])
def test_public_url_subpath_is_preserved(public_url):
    assert (handle_public_url('http://127.0.0.1:6800/logs/demo/test/a.log', public_url)
            == 'https://crawl.example.com/scrapyd-1/logs/demo/test/a.log')
    assert (handle_public_url('http://127.0.0.1:6800/items/demo/test/', public_url)
            == 'https://crawl.example.com/scrapyd-1/items/demo/test/')


# --------------------------------------------------------------------------- #
# Defensive: input that is not an http(s) URL is returned unchanged instead of
# being mangled.
# --------------------------------------------------------------------------- #
def test_non_http_url_is_returned_unchanged():
    assert handle_public_url('not-a-url/logs/x', PUBLIC) == 'not-a-url/logs/x'


# --------------------------------------------------------------------------- #
# The Jobs page resolves per-job links with urljoin() against the public base;
# verify the absolute Scrapyd hrefs resolve onto the public host (this is the
# exact mechanism used in jobs.py).
# --------------------------------------------------------------------------- #
def test_jobs_urljoin_against_public_base():
    public_jobs = handle_public_url('http://%s/jobs' % SERVER, PUBLIC)
    assert public_jobs == 'https://crawl.example.com/jobs'
    assert (urljoin(public_jobs, '/logs/demo/test/job.log')
            == 'https://crawl.example.com/logs/demo/test/job.log')
    assert (urljoin(public_jobs, '/items/demo/test/job.jl')
            == 'https://crawl.example.com/items/demo/test/job.jl')

    # Without a public URL the same resolution must stay on the internal host.
    internal_jobs = handle_public_url('http://%s/jobs' % SERVER, '')
    assert (urljoin(internal_jobs, '/logs/demo/test/job.log')
            == 'http://127.0.0.1:6800/logs/demo/test/job.log')


# --------------------------------------------------------------------------- #
# BaseView.make_public_url() must delegate to handle_public_url() using the
# per-node SCRAPYD_SERVER_PUBLIC_URL. Call it as an unbound method against a tiny
# stub so no Flask app / request context / database is needed.
# --------------------------------------------------------------------------- #
def test_baseview_make_public_url_delegates():
    from scrapydweb.views.baseview import BaseView

    class _Stub(object):
        SCRAPYD_SERVER_PUBLIC_URL = PUBLIC

    stub = _Stub()
    assert (BaseView.make_public_url(stub, 'http://127.0.0.1:6800/logs/x.log')
            == 'https://crawl.example.com/logs/x.log')

    # No public URL -> direct semantics preserved.
    stub.SCRAPYD_SERVER_PUBLIC_URL = ''
    assert (BaseView.make_public_url(stub, 'http://127.0.0.1:6800/logs/x.log')
            == 'http://127.0.0.1:6800/logs/x.log')
