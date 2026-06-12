# coding: utf-8
"""
Unit tests for scrapydweb/views/operations/candidate.py

Tests the scrapy.cfg candidate discovery, selection, parsing, and egg naming
utilities in isolation (no Flask app or scrapyd server required).
"""
import io
import os
import shutil
import tempfile

import pytest

from scrapydweb.views.operations.candidate import (
    AmbiguousCandidateError,
    discover_scrapy_cfg_candidates,
    generate_egg_filename,
    select_candidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_scrapy_cfg(directory, project='demo', settings='demo.settings',
                      deploy_url='http://localhost:6800/'):
    """Write a minimal scrapy.cfg into *directory*."""
    os.makedirs(directory, exist_ok=True)
    cfg_path = os.path.join(directory, 'scrapy.cfg')
    content = (
        "[settings]\n"
        "default = {settings}\n"
        "\n"
        "[deploy]\n"
        "url = {url}\n"
        "project = {project}\n"
    ).format(settings=settings, url=deploy_url, project=project)
    with io.open(cfg_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return cfg_path


@pytest.fixture
def project_tree(tmp_path):
    """
    Build a small project tree with multiple scrapy.cfg files at various depths.

    Layout:
        root/
            scrapy.cfg                   (root-level)
            projectA/
                scrapy.cfg
            projectB/
                scrapy.cfg
            nested/
                deep/
                    scrapy.cfg
            build/                       (should be skipped)
                scrapy.cfg
            projectC.egg-info/           (should be skipped)
                scrapy.cfg
    """
    root = str(tmp_path)

    _write_scrapy_cfg(root, project='root_project', settings='root.settings')
    _write_scrapy_cfg(os.path.join(root, 'projectA'), project='alpha', settings='alpha.settings')
    _write_scrapy_cfg(os.path.join(root, 'projectB'), project='beta', settings='beta.settings')
    _write_scrapy_cfg(os.path.join(root, 'nested', 'deep'), project='deep', settings='deep.settings')

    # These should be pruned
    _write_scrapy_cfg(os.path.join(root, 'build'), project='build_proj', settings='build.settings')
    _write_scrapy_cfg(os.path.join(root, 'projectC.egg-info'), project='egginfo', settings='ei.settings')

    return root


# ---------------------------------------------------------------------------
# discover_scrapy_cfg_candidates
# ---------------------------------------------------------------------------

class TestDiscover:

    def test_finds_all_levels(self, project_tree):
        """Projects at root, one-level, and deeply nested are all found."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        rel_paths = [c['relative_path'] for c in candidates]
        assert 'scrapy.cfg' in rel_paths                      # root-level
        assert os.path.join('projectA', 'scrapy.cfg') in rel_paths
        assert os.path.join('nested', 'deep', 'scrapy.cfg') in rel_paths

    def test_prunes_build_dirs(self, project_tree):
        """Directories named 'build' and '*.egg-info' are skipped."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        rel_paths = [c['relative_path'] for c in candidates]
        assert os.path.join('build', 'scrapy.cfg') not in rel_paths
        assert os.path.join('projectC.egg-info', 'scrapy.cfg') not in rel_paths

    def test_empty_directory(self, tmp_path):
        """Returns empty list when directory has no scrapy.cfg."""
        root = str(tmp_path)
        os.makedirs(os.path.join(root, 'empty_sub'), exist_ok=True)
        candidates = discover_scrapy_cfg_candidates(root)
        assert candidates == []

    def test_nonexistent_path(self):
        """Returns empty list when path doesn't exist."""
        candidates = discover_scrapy_cfg_candidates('/nonexistent/path/xyz123')
        assert candidates == []

    def test_sorted_by_relative_path(self, project_tree):
        """Results are sorted case-insensitively by relative_path."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        rel_paths = [c['relative_path'] for c in candidates]
        assert rel_paths == sorted(rel_paths, key=str.lower)

    def test_exactly_four_candidates(self, project_tree):
        """With pruning, exactly 4 valid candidates remain."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        assert len(candidates) == 4


# ---------------------------------------------------------------------------
# _parse_candidate_metadata (tested via discover)
# ---------------------------------------------------------------------------

class TestParseMetadata:

    def test_valid_cfg(self, tmp_path):
        """Correctly extracts project_name and settings_module."""
        root = str(tmp_path)
        _write_scrapy_cfg(os.path.join(root, 'myproject'),
                          project='myproject', settings='myproject.settings')
        candidates = discover_scrapy_cfg_candidates(root)
        assert len(candidates) == 1
        c = candidates[0]
        assert c['project_name'] == 'myproject'
        assert c['settings_module'] == 'myproject.settings'

    def test_missing_deploy_project_fallback(self, tmp_path):
        """Falls back to directory basename when [deploy] project is missing."""
        root = str(tmp_path)
        proj_dir = os.path.join(root, 'fallback_name')
        os.makedirs(proj_dir, exist_ok=True)
        cfg_path = os.path.join(proj_dir, 'scrapy.cfg')
        with io.open(cfg_path, 'w', encoding='utf-8') as f:
            f.write("[settings]\ndefault = demo.settings\n")
        candidates = discover_scrapy_cfg_candidates(root)
        assert len(candidates) == 1
        assert candidates[0]['project_name'] == 'fallback_name'

    def test_broken_cfg(self, tmp_path):
        """Handles parse errors gracefully — candidate still appears with fallback name."""
        root = str(tmp_path)
        proj_dir = os.path.join(root, 'broken_proj')
        os.makedirs(proj_dir, exist_ok=True)
        cfg_path = os.path.join(proj_dir, 'scrapy.cfg')
        with io.open(cfg_path, 'w', encoding='utf-8') as f:
            f.write("this is not valid ini\n[broken\n")
        candidates = discover_scrapy_cfg_candidates(root)
        assert len(candidates) == 1
        assert candidates[0]['project_name'] == 'broken_proj'
        assert candidates[0]['settings_module'] == ''

    def test_no_section_settings(self, tmp_path):
        """Missing [settings] section is handled gracefully."""
        root = str(tmp_path)
        proj_dir = os.path.join(root, 'nosection')
        os.makedirs(proj_dir, exist_ok=True)
        cfg_path = os.path.join(proj_dir, 'scrapy.cfg')
        with io.open(cfg_path, 'w', encoding='utf-8') as f:
            f.write("[deploy]\nproject = testproj\n")
        candidates = discover_scrapy_cfg_candidates(root)
        assert len(candidates) == 1
        assert candidates[0]['project_name'] == 'testproj'
        assert candidates[0]['settings_module'] == ''


# ---------------------------------------------------------------------------
# select_candidate
# ---------------------------------------------------------------------------

class TestSelectCandidate:

    def test_auto_single(self, tmp_path):
        """Auto-selects when exactly 1 candidate exists."""
        root = str(tmp_path)
        _write_scrapy_cfg(os.path.join(root, 'proj'), project='proj')
        candidates = discover_scrapy_cfg_candidates(root)
        result = select_candidate(candidates)
        assert result is not None
        assert result['project_name'] == 'proj'

    def test_explicit_by_relative_path(self, project_tree):
        """Selects specific candidate by relative_path."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        target_rel = os.path.join('projectA', 'scrapy.cfg')
        result = select_candidate(candidates, selected_path=target_rel)
        assert result is not None
        assert result['project_name'] == 'alpha'

    def test_explicit_by_absolute_path(self, project_tree):
        """Selects specific candidate by absolute scrapy_cfg_path."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        target_abs = os.path.abspath(os.path.join(project_tree, 'projectB', 'scrapy.cfg'))
        result = select_candidate(candidates, selected_path=target_abs)
        assert result is not None
        assert result['project_name'] == 'beta'

    def test_ambiguous_raises(self, project_tree):
        """Raises AmbiguousCandidateError when multiple candidates, no selection."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        assert len(candidates) > 1
        with pytest.raises(AmbiguousCandidateError) as exc_info:
            select_candidate(candidates)
        assert exc_info.value.candidates == candidates

    def test_no_candidates(self):
        """Returns None when candidate list is empty."""
        result = select_candidate([])
        assert result is None

    def test_explicit_no_match(self, project_tree):
        """Returns None when selected_path doesn't match any candidate."""
        candidates = discover_scrapy_cfg_candidates(project_tree)
        result = select_candidate(candidates, selected_path='nonexistent/scrapy.cfg')
        assert result is None


# ---------------------------------------------------------------------------
# generate_egg_filename
# ---------------------------------------------------------------------------

class TestGenerateEggFilename:

    def test_basic(self):
        assert generate_egg_filename('demo', '2024-01-01T01_01_01') == 'demo_2024-01-01T01_01_01.egg'

    def test_unicode_project(self):
        result = generate_egg_filename(u'demo_副本', '1.0')
        assert result == u'demo_副本_1.0.egg'

    def test_version_with_dashes(self):
        result = generate_egg_filename('proj', 'v2-beta-rc1')
        assert result == 'proj_v2-beta-rc1.egg'
