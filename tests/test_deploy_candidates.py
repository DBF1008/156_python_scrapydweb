# coding: utf-8
"""
Regression tests for candidate-aware deployment (multiple scrapy.cfg discovery + explicit
selection) and the DEPLOY_PATH-only egg landing rule.

These tests build their fixtures at runtime (temp project folders under SCRAPY_PROJECTS_DIR and
in-memory zip archives), so they do not touch the binary tests/data.zip. The chooser and
invalid-selection cases short-circuit before any Scrapyd call; the explicit-selection cases assert
on the built egg in DEPLOY_PATH, which is produced before the deploy network call.
"""
from io import BytesIO
import os
import re
from shutil import copytree, rmtree
import zipfile

from scrapydweb.vars import DEPLOY_PATH, LEGAL_NAME_PATTERN, STRICT_NAME_PATTERN
from tests.utils import cst, req


DATA_DIR = os.path.join(cst.ROOT_DIR, 'data')
DEMO_SRC = os.path.join(DATA_DIR, 'demo')  # a buildable scrapy project extracted from data.zip
CHOOSER_MARKER = 'Multiple scrapy.cfg candidates'


def _make_local_project(folder, subdirs):
    """Create tests/data/<folder>/<sub> as a copy of the demo project for each sub in subdirs."""
    root = os.path.join(DATA_DIR, folder)
    rmtree(root, ignore_errors=True)
    for sub in subdirs:
        copytree(DEMO_SRC, os.path.join(root, sub))
    return root


def _zip_project(subdirs):
    """Return the bytes of a zip with the demo project copied under each sub in subdirs."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for sub in subdirs:
            for dirpath, _dirnames, filenames in os.walk(DEMO_SRC):
                for filename in filenames:
                    abspath = os.path.join(dirpath, filename)
                    rel = os.path.relpath(abspath, DEMO_SRC).replace('\\', '/')
                    zf.write(abspath, '%s/%s' % (sub, rel))
    buf.seek(0)
    return buf.getvalue()


def _local_eggname(project, version, sub=None):
    name = '%s_%s' % (re.sub(STRICT_NAME_PATTERN, '_', project), re.sub(LEGAL_NAME_PATTERN, '-', version))
    if sub:
        name += '__%s' % re.sub(STRICT_NAME_PATTERN, '_', sub)
    return name + '.egg'


def _reset_egg(eggname):
    eggpath = os.path.join(DEPLOY_PATH, eggname)
    if os.path.exists(eggpath):
        os.remove(eggpath)
    return eggpath


# --- local auto-packaging -----------------------------------------------------------------------

def test_local_multiple_candidates_lists_them(app, client):
    folder = 'cand_local_multi'
    root = _make_local_project(folder, ['projA', 'projB'])
    try:
        data = {'1': 'on', 'checked_amount': '1', 'folder': folder,
                'project': 'multi', 'version': cst.VERSION}
        # No scrapy_cfg selected -> ambiguous -> the chooser must list every candidate and not deploy.
        req(app, client, view='deploy.upload', kws=dict(node=2), data=data,
            ins=[CHOOSER_MARKER, 'projA/scrapy.cfg', 'projB/scrapy.cfg',
                 'name="folder" value="%s"' % folder, 'name="scrapy_cfg"',
                 'name="checked_amount"', 'name="1"'],
            nos=['deploy results - ScrapydWeb', 'fail - ScrapydWeb'])
    finally:
        rmtree(root, ignore_errors=True)


def test_local_multiple_candidates_explicit_selection(app, client):
    folder = 'cand_local_select'
    root = _make_local_project(folder, ['projA', 'projB'])
    eggname = _local_eggname('multi', cst.VERSION, sub='projB')  # >1 candidate + nested -> suffixed
    eggpath = _reset_egg(eggname)
    try:
        data = {'1': 'on', 'checked_amount': '1', 'folder': folder,
                'project': 'multi', 'version': cst.VERSION,
                'scrapy_cfg': 'projB/scrapy.cfg'}
        text, __ = req(app, client, view='deploy.upload', kws=dict(node=2), data=data,
                       nos=CHOOSER_MARKER)
        assert os.path.exists(eggpath), \
            "expected disambiguated egg %s in DEPLOY_PATH, got %s" % (eggname, os.listdir(DEPLOY_PATH))
    finally:
        rmtree(root, ignore_errors=True)
        if os.path.exists(eggpath):
            os.remove(eggpath)


def test_local_invalid_selection(app, client):
    folder = 'cand_local_invalid'
    root = _make_local_project(folder, ['projA', 'projB'])
    try:
        data = {'1': 'on', 'checked_amount': '1', 'folder': folder,
                'project': 'multi', 'version': cst.VERSION,
                'scrapy_cfg': 'does/not/exist/scrapy.cfg'}
        req(app, client, view='deploy.upload', kws=dict(node=2), data=data,
            ins=['fail - ScrapydWeb', 'not found among the discovered candidates', 'projA/scrapy.cfg'],
            nos=[CHOOSER_MARKER, 'deploy results - ScrapydWeb'])
    finally:
        rmtree(root, ignore_errors=True)


def test_local_single_candidate_no_regression(app, client):
    # A single nested candidate must still auto-build with the canonical (unsuffixed) name.
    folder = 'cand_local_single'
    root = _make_local_project(folder, ['projA'])
    eggname = _local_eggname('single', cst.VERSION)  # 1 candidate -> no suffix
    eggpath = _reset_egg(eggname)
    try:
        data = {'1': 'on', 'checked_amount': '1', 'folder': folder,
                'project': 'single', 'version': cst.VERSION}
        req(app, client, view='deploy.upload', kws=dict(node=2), data=data, nos=CHOOSER_MARKER)
        assert os.path.exists(eggpath), \
            "expected canonical egg %s in DEPLOY_PATH, got %s" % (eggname, os.listdir(DEPLOY_PATH))
    finally:
        rmtree(root, ignore_errors=True)
        if os.path.exists(eggpath):
            os.remove(eggpath)


def test_egg_not_written_into_source_tree(app, client):
    # The DEPLOY_PATH-only landing rule: building must not drop a .egg into the scanned source tree.
    folder = 'cand_local_landing'
    root = _make_local_project(folder, ['projA'])
    eggname = _local_eggname('landing', cst.VERSION)
    eggpath = _reset_egg(eggname)
    try:
        data = {'1': 'on', 'checked_amount': '1', 'folder': folder,
                'project': 'landing', 'version': cst.VERSION}
        req(app, client, view='deploy.upload', kws=dict(node=2), data=data, nos=CHOOSER_MARKER)
        assert os.path.exists(eggpath), "egg should land in DEPLOY_PATH"
        stray_eggs = []
        for dirpath, _dirnames, filenames in os.walk(root):
            stray_eggs.extend(os.path.join(dirpath, f) for f in filenames if f.endswith('.egg'))
        assert not stray_eggs, "no egg should be written into the scanned source tree: %s" % stray_eggs
    finally:
        rmtree(root, ignore_errors=True)
        if os.path.exists(eggpath):
            os.remove(eggpath)


# --- uploaded compressed file -------------------------------------------------------------------

def test_upload_multiple_candidates_lists_them(app, client):
    data = {
        '1': 'on', 'checked_amount': '1',
        'project': 'multiupload', 'version': cst.VERSION,
        'file': (BytesIO(_zip_project(['proj1', 'proj2'])), 'multi.zip'),
    }
    # No scrapy_cfg selected -> chooser; it must carry uploaded_filename so re-submit needs no re-upload.
    req(app, client, view='deploy.upload', kws=dict(node=2), data=data,
        ins=[CHOOSER_MARKER, 'proj1/scrapy.cfg', 'proj2/scrapy.cfg',
             'name="uploaded_filename"', 'multi.zip', 'name="scrapy_cfg"'],
        nos=['deploy results - ScrapydWeb', 'fail - ScrapydWeb'])


def test_upload_multiple_candidates_explicit_selection(app, client):
    version = re.sub(LEGAL_NAME_PATTERN, '-', cst.VERSION)
    base = 'multiupload_%s_from_file_multi.egg' % version
    eggname = '%s__proj2.egg' % base[:-len('.egg')]  # >1 candidate + nested -> suffixed
    eggpath = _reset_egg(eggname)
    try:
        data = {
            '1': 'on', 'checked_amount': '1',
            'project': 'multiupload', 'version': cst.VERSION,
            'scrapy_cfg': 'proj2/scrapy.cfg',
            'file': (BytesIO(_zip_project(['proj1', 'proj2'])), 'multi.zip'),
        }
        # Selection sent together with the upload -> build the chosen candidate in one shot.
        req(app, client, view='deploy.upload', kws=dict(node=2), data=data, nos=CHOOSER_MARKER)
        assert os.path.exists(eggpath), \
            "expected disambiguated egg %s in DEPLOY_PATH, got %s" % (eggname, os.listdir(DEPLOY_PATH))
    finally:
        if os.path.exists(eggpath):
            os.remove(eggpath)


def test_upload_reselect_two_step(app, client):
    # The chooser persists the uploaded archive in DEPLOY_PATH; re-submitting the chosen candidate
    # must rebuild from it without requiring the file to be uploaded again.
    version = re.sub(LEGAL_NAME_PATTERN, '-', cst.VERSION)
    uploaded_filename = 'multiupload_%s_from_file_multi.zip' % version
    base = 'multiupload_%s_from_file_multi.egg' % version
    eggname = '%s__proj1.egg' % base[:-len('.egg')]
    eggpath = _reset_egg(eggname)
    try:
        # Step 1: upload with no selection -> chooser carrying the persisted archive name.
        req(app, client, view='deploy.upload', kws=dict(node=2),
            data={'1': 'on', 'checked_amount': '1', 'project': 'multiupload', 'version': cst.VERSION,
                  'file': (BytesIO(_zip_project(['proj1', 'proj2'])), 'multi.zip')},
            ins=[CHOOSER_MARKER, 'name="uploaded_filename" value="%s"' % uploaded_filename])
        assert os.path.exists(os.path.join(DEPLOY_PATH, uploaded_filename)), \
            "the uploaded archive should be persisted in DEPLOY_PATH for re-submission"
        # Step 2: re-submit the chosen candidate without re-uploading the file.
        req(app, client, view='deploy.upload', kws=dict(node=2),
            data={'1': 'on', 'checked_amount': '1', 'project': 'multiupload', 'version': cst.VERSION,
                  'uploaded_filename': uploaded_filename, 'scrapy_cfg': 'proj1/scrapy.cfg'},
            nos=CHOOSER_MARKER)
        assert os.path.exists(eggpath), \
            "re-submission should build the chosen candidate egg %s, got %s" % (eggname, os.listdir(DEPLOY_PATH))
    finally:
        if os.path.exists(eggpath):
            os.remove(eggpath)
