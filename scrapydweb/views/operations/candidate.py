# coding: utf-8
"""
Scrapy.cfg candidate discovery and selection utilities.

Provides recursive discovery of all scrapy.cfg files in a directory tree,
parsing of project metadata, and explicit candidate selection for the
deployment pipeline.
"""
import logging
import os
import sys

from six import text_type

from .scrapyd_deploy import get_config

try:
    from six.moves.configparser import Error as ScrapyCfgParseError
except ImportError:
    from configparser import Error as ScrapyCfgParseError


logger = logging.getLogger(__name__)

PY2 = sys.version_info.major < 3

# Directories to skip when walking project trees
_SKIP_DIRS = {'build', '__pycache__', '.git', 'node_modules', '.svn', '.hg'}


def _should_skip_dir(dirname):
    """Check if a directory should be skipped during discovery."""
    if dirname in _SKIP_DIRS:
        return True
    if dirname.endswith('.egg-info'):
        return True
    return False


def _safe_walk(top, topdown=True, onerror=None, followlinks=False):
    """
    Unicode-safe os.walk replacement for Python 2.
    Skips non-unicode filenames that would cause UnicodeDecodeError.
    Based on BaseView.safe_walk but standalone (no self dependency).
    """
    islink, join, isdir = os.path.islink, os.path.join, os.path.isdir

    try:
        names = os.listdir(top)
    except OSError as err:
        if onerror is not None:
            onerror(err)
        return

    new_names = []
    for name in names:
        if isinstance(name, text_type):
            new_names.append(name)
        else:
            logger.warning("Ignore non-unicode filename %s in %s", repr(name), top)
    names = new_names

    dirs, nondirs = [], []
    for name in names:
        if isdir(join(top, name)):
            dirs.append(name)
        else:
            nondirs.append(name)

    if topdown:
        yield top, dirs, nondirs
    for name in dirs:
        new_path = join(top, name)
        if followlinks or not islink(new_path):
            for x in _safe_walk(new_path, topdown, onerror, followlinks):
                yield x
    if not topdown:
        yield top, dirs, nondirs


def _parse_candidate_metadata(scrapy_cfg_path, search_root):
    """
    Parse a scrapy.cfg file and return a candidate dict.

    Returns dict with keys:
        scrapy_cfg_path: absolute path to scrapy.cfg
        project_name: from [deploy] project, fallback to parent dir basename
        settings_module: from [settings] default
        relative_path: relative path from search_root to scrapy.cfg
    """
    abs_path = os.path.abspath(scrapy_cfg_path)
    rel_path = os.path.relpath(abs_path, search_root)
    parent_dir = os.path.basename(os.path.dirname(abs_path))

    project_name = parent_dir  # Default fallback
    settings_module = ''

    try:
        cfg = get_config(abs_path)
        try:
            project_name = cfg.get('deploy', 'project') or parent_dir
        except (ScrapyCfgParseError, Exception):
            pass
        try:
            settings_module = cfg.get('settings', 'default') or ''
        except (ScrapyCfgParseError, Exception):
            pass
    except (ScrapyCfgParseError, Exception) as err:
        logger.warning("Failed to parse %s: %s", abs_path, err)

    return {
        'scrapy_cfg_path': abs_path,
        'project_name': project_name,
        'settings_module': settings_module,
        'relative_path': rel_path,
    }


def discover_scrapy_cfg_candidates(search_path):
    """
    Recursively discover ALL scrapy.cfg files under search_path.

    Args:
        search_path: Root directory to search in.

    Returns:
        List of candidate dicts sorted by relative_path (case-insensitive).
        Each dict has: scrapy_cfg_path, project_name, settings_module, relative_path.
        Returns empty list if search_path doesn't exist or contains no scrapy.cfg.
    """
    search_path = os.path.abspath(search_path)
    if not os.path.isdir(search_path):
        logger.warning("Search path does not exist: %s", search_path)
        return []

    candidates = []
    walked_paths = set()

    def _walk(func_walk):
        for dirpath, dirnames, filenames in func_walk(search_path):
            # Prune irrelevant directories (modify in-place for os.walk)
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

            abs_dirpath = os.path.abspath(dirpath)
            if abs_dirpath in walked_paths:
                continue
            walked_paths.add(abs_dirpath)

            if 'scrapy.cfg' in filenames:
                scrapy_cfg_path = os.path.join(dirpath, 'scrapy.cfg')
                candidate = _parse_candidate_metadata(scrapy_cfg_path, search_path)
                candidates.append(candidate)

    try:
        _walk(os.walk)
    except UnicodeDecodeError:
        logger.warning("UnicodeDecodeError during walk, falling back to safe_walk")
        candidates = []
        walked_paths = set()
        _walk(_safe_walk)

    candidates.sort(key=lambda c: c['relative_path'].lower())
    return candidates


class AmbiguousCandidateError(Exception):
    """Raised when multiple scrapy.cfg candidates found but none explicitly selected."""

    def __init__(self, candidates):
        self.candidates = candidates
        msg = "Found %d scrapy.cfg files; explicit selection required. Candidates:\n" % len(candidates)
        for c in candidates:
            msg += "  - %s (project: %s)\n" % (c['relative_path'], c['project_name'])
        super(AmbiguousCandidateError, self).__init__(msg)


def select_candidate(candidates, selected_path=None):
    """
    Select a single candidate from the list.

    Args:
        candidates: List of candidate dicts from discover_scrapy_cfg_candidates().
        selected_path: Optional path to match (by scrapy_cfg_path or relative_path).

    Returns:
        Selected candidate dict, or None if no candidates.

    Raises:
        AmbiguousCandidateError: When multiple candidates exist without explicit selection.
    """
    if not candidates:
        return None

    if len(candidates) == 1 and not selected_path:
        return candidates[0]

    if selected_path:
        # Try exact match by absolute path or relative path
        for c in candidates:
            if c['scrapy_cfg_path'] == os.path.abspath(selected_path):
                return c
            if c['relative_path'] == selected_path:
                return c
        # Try basename match as last resort
        selected_basename = os.path.basename(os.path.dirname(selected_path))
        for c in candidates:
            parent = os.path.basename(os.path.dirname(c['scrapy_cfg_path']))
            if parent == selected_basename:
                return c
        # No match found
        return None

    # Multiple candidates, no explicit selection
    raise AmbiguousCandidateError(candidates)


def generate_egg_filename(project, version):
    """
    Generate consistent egg filename: {project}_{version}.egg

    Args:
        project: Project name string.
        version: Version string.

    Returns:
        Filename string in format '{project}_{version}.egg'.
    """
    return '%s_%s.egg' % (project, version)
