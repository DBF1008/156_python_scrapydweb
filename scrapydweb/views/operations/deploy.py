# coding: utf-8
from datetime import datetime
import glob
import io
import os
from pprint import pformat
import re
from shutil import copyfile, copyfileobj, rmtree
from subprocess import CalledProcessError
import tarfile
import tempfile
import time
import zipfile

from flask import flash, redirect, render_template, request, url_for
from six import text_type
from six.moves.configparser import Error as ScrapyCfgParseError
from werkzeug.utils import secure_filename

from ...vars import PY2
from ..baseview import BaseView
from .scrapyd_deploy import _build_egg, get_config
from .utils import mkdir_p, slot


SCRAPY_CFG = """
[settings]
default = projectname.settings

[deploy]
url = http://localhost:6800/
project = projectname

"""
folder_project_dict = {}


class DeployView(BaseView):

    def __init__(self):
        super(DeployView, self).__init__()

        self.url = 'http://{}/{}.json'.format(self.SCRAPYD_SERVER, 'addversion')
        self.template = 'scrapydweb/deploy.html'

        self.scrapy_cfg_list = []
        self.project_paths = []
        self.folders = []
        self.projects = []
        self.modification_times = []
        self.latest_folder = ''

    def dispatch_request(self, **kwargs):
        self.set_scrapy_cfg_list()
        self.project_paths = [os.path.dirname(i) for i in self.scrapy_cfg_list]
        self.folders = [os.path.basename(i) for i in self.project_paths]
        self.get_modification_times()
        self.parse_scrapy_cfg()

        kwargs = dict(
            node=self.node,
            url=self.url,
            url_projects=url_for('projects', node=self.node),
            selected_nodes=self.get_selected_nodes(),
            folders=self.folders,
            projects=self.projects,
            modification_times=self.modification_times,
            latest_folder=self.latest_folder,
            SCRAPY_PROJECTS_DIR=self.SCRAPY_PROJECTS_DIR.replace('\\', '/'),
            url_servers=url_for('servers', node=self.node, opt='deploy'),
            url_deploy_upload=url_for('deploy.upload', node=self.node)
        )
        return render_template(self.template, **kwargs)

    def set_scrapy_cfg_list(self):
        # Python 'ascii' codec can't decode byte
        try:
            self.scrapy_cfg_list = glob.glob(os.path.join(self.SCRAPY_PROJECTS_DIR, '*', u'scrapy.cfg'))
        except UnicodeDecodeError:
            if PY2:
                for name in os.listdir(os.path.join(self.SCRAPY_PROJECTS_DIR, u'')):
                    if not isinstance(name, text_type):
                        msg = "Ignore non-unicode filename %s in %s" % (repr(name), self.SCRAPY_PROJECTS_DIR)
                        self.logger.error(msg)
                        flash(msg, self.WARN)
                    else:
                        scrapy_cfg = os.path.join(self.SCRAPY_PROJECTS_DIR, name, u'scrapy.cfg')
                        if os.path.exists(scrapy_cfg):
                            self.scrapy_cfg_list.append(scrapy_cfg)
            else:
                raise
        # '/home/username/Downloads/scrapydweb/scrapydweb/data/demo_projects/\udc8b\udc8billegal/scrapy.cfg'
        # UnicodeEncodeError: 'utf-8' codec can't encode characters in position 64-65: surrogates not allowed
        new_scrapy_cfg_list = []
        for scrapy_cfg in self.scrapy_cfg_list:
            try:
                scrapy_cfg.encode('utf-8')
            except UnicodeEncodeError:
                msg = "Ignore scrapy.cfg in illegal pathname %s" % repr(os.path.dirname(scrapy_cfg))
                self.logger.error(msg)
                flash(msg, self.WARN)
            else:
                new_scrapy_cfg_list.append(scrapy_cfg)
        self.scrapy_cfg_list = new_scrapy_cfg_list

        self.scrapy_cfg_list.sort(key=lambda x: x.lower())

    def get_modification_times(self):
        timestamps = [self.get_modification_time(path) for path in self.project_paths]
        self.modification_times = [datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H_%M_%S') for ts in timestamps]

        if timestamps:
            max_timestamp_index = timestamps.index(max(timestamps))
            self.latest_folder = self.folders[max_timestamp_index]
            self.logger.debug('latest_folder: %s', self.latest_folder)

    def get_modification_time(self, path, func_walk=os.walk, retry=True):
        # https://stackoverflow.com/a/29685234/10517783
        # https://stackoverflow.com/a/13454267/10517783
        filepath_list = []
        in_top_dir = True
        try:
            for dirpath, dirnames, filenames in func_walk(path):
                if in_top_dir:
                    in_top_dir = False
                    dirnames[:] = [d for d in dirnames if d not in ['build', 'project.egg-info']]
                    filenames = [f for f in filenames
                                 if not (f.endswith('.egg') or f in ['setup.py', 'setup_backup.py'])]
                for filename in filenames:
                    filepath_list.append(os.path.join(dirpath, filename))
        except UnicodeDecodeError:
            msg = "Found illegal filenames in %s" % path
            self.logger.error(msg)
            flash(msg, self.WARN)
            if PY2 and retry:
                return self.get_modification_time(path, func_walk=self.safe_walk, retry=False)
            else:
                raise
        else:
            return max([os.path.getmtime(f) for f in filepath_list] or [time.time()])

    def parse_scrapy_cfg(self):
        for (idx, scrapy_cfg) in enumerate(self.scrapy_cfg_list):
            folder = self.folders[idx]
            key = '%s (%s)' % (folder, self.modification_times[idx])

            project = folder_project_dict.get(key, '')
            if project:
                self.projects.append(project)
                self.logger.debug('Hit %s, project %s', key, project)
                continue
            else:
                project = folder
                try:
                    # lib/configparser.py: def get(self, section, option, *, raw=False, vars=None, fallback=_UNSET):
                    # projectname/scrapy.cfg: [deploy] project = demo
                    # PY2: get() got an unexpected keyword argument 'fallback'
                    # project = get_config(scrapy_cfg).get('deploy', 'project', fallback=folder) or folder
                    project = get_config(scrapy_cfg).get('deploy', 'project')
                except ScrapyCfgParseError as err:
                    self.logger.error("%s parse error: %s", scrapy_cfg, err)
                finally:
                    project = project or folder
                    self.projects.append(project)
                    folder_project_dict[key] = project
                    self.logger.debug('Add %s, project %s', key, project)

        keys_all = list(folder_project_dict.keys())
        keys_exist = ['%s (%s)' % (_folder, _modification_time)
                      for (_folder, _modification_time) in zip(self.folders, self.modification_times)]
        diff = set(keys_all).difference(set(keys_exist))
        for key in diff:
            self.logger.debug('Pop %s, project %s', key, folder_project_dict.pop(key))
        self.logger.debug(self.json_dumps(folder_project_dict))
        self.logger.debug('folder_project_dict length: %s', len(folder_project_dict))


class DeployUploadView(BaseView):
    methods = ['POST']

    def __init__(self):
        super(DeployUploadView, self).__init__()

        self.url = ''
        self.template = 'scrapydweb/deploy_results.html'

        self.folder = ''
        self.project = ''
        self.version = ''
        self.selected_nodes_amount = 0
        self.selected_nodes = []
        self.first_selected_node = 0

        self.eggname = ''
        self.eggpath = ''
        self.scrapy_cfg_path = ''
        self.scrapy_cfg_searched_paths = []
        self.scrapy_cfg_not_found = False
        self.scrapy_cfg_parse_error = ''
        self.build_egg_subprocess_error = ''
        # Candidate discovery: collect every scrapy.cfg under the search root so the caller
        # can see them and explicitly pick which one to build (instead of silently using the
        # first one hit while walking).
        self.scrapy_cfg_search_root = ''
        self.scrapy_cfg_candidates = []  # [{'rel': ..., 'path': ..., 'project': ...}, ...]
        self.scrapy_cfg_selected = ''  # relative path picked by the caller, if any
        self.scrapy_cfg_chosen_rel = ''  # relative path actually resolved for building
        self.scrapy_cfg_multiple = False  # >1 candidate and none selected -> show chooser
        self.scrapy_cfg_invalid_selection = False  # selected path matched no candidate
        self.uploaded_filename = ''  # archive persisted in DEPLOY_PATH, reused on re-submit
        self.uncompress_tmpdir = ''
        self.data = None
        self.js = {}

        self.slot = slot

    def dispatch_request(self, **kwargs):
        self.handle_form()

        if self.scrapy_cfg_multiple:
            return render_template(
                'scrapydweb/deploy_candidates.html',
                node=self.node,
                candidates=self.scrapy_cfg_candidates,
                folder=self.folder,
                uploaded_filename=self.uploaded_filename,
                project=self.project,
                version=self.version,
                selected_nodes=self.selected_nodes,
                selected_nodes_amount=self.selected_nodes_amount,
                SCRAPYD_SERVERS_AMOUNT=self.SCRAPYD_SERVERS_AMOUNT,
                url_deploy_upload=url_for('deploy.upload', node=self.node),
            )

        if self.scrapy_cfg_invalid_selection:
            if self.selected_nodes_amount > 1:
                alert = "Multinode deployment terminated:"
            else:
                alert = "Fail to deploy project:"
            text = "The selected scrapy.cfg was not found among the discovered candidates"
            tip = "Select one of the candidates listed below, then deploy again. "
            message = "candidates:\n%s" % pformat([c['rel'] for c in self.scrapy_cfg_candidates])
            return render_template(self.template_fail, node=self.node,
                                   alert=alert, text=text, tip=tip, message=message)

        if self.scrapy_cfg_not_found or self.scrapy_cfg_parse_error or self.build_egg_subprocess_error:
            if self.selected_nodes_amount > 1:
                alert = "Multinode deployment terminated:"
            else:
                alert = "Fail to deploy project:"

            if self.scrapy_cfg_not_found:
                text = "scrapy.cfg not found"
                tip = "Make sure that the 'scrapy.cfg' file resides in your project directory. "
            elif self.scrapy_cfg_parse_error:
                text = self.scrapy_cfg_parse_error
                tip = "Check the content of the 'scrapy.cfg' file in your project directory. "
            else:
                text = self.build_egg_subprocess_error
                tip = ("Check the content of the 'scrapy.cfg' file in your project directory. "
                       "Or build the egg file by yourself instead. ")

            if self.scrapy_cfg_not_found:
                # Handle case when scrapy.cfg not found in zip file which contains illegal pathnames in PY3
                message = "scrapy_cfg_searched_paths:\n%s" % pformat(self.scrapy_cfg_searched_paths)
            else:
                message = "# The 'scrapy.cfg' file in your project directory should be like:\n%s" % SCRAPY_CFG

            return render_template(self.template_fail, node=self.node,
                                   alert=alert, text=text, tip=tip, message=message)
        else:
            self.prepare_data()
            status_code, self.js = self.make_request(self.url, data=self.data, auth=self.AUTH)

        if self.js['status'] != self.OK:
            # With multinodes, would try to deploy to the first selected node first
            if self.selected_nodes_amount > 1:
                alert = ("Multinode deployment terminated, "
                         "since the first selected node returned status: " + self.js['status'])
            else:
                alert = "Fail to deploy project, got status: " + self.js['status']
            message = self.js.get('message', '')
            if message:
                self.js['message'] = 'See details below'

            return render_template(self.template_fail, node=self.node,
                                   alert=alert, text=self.json_dumps(self.js), message=message)
        else:
            if self.selected_nodes_amount == 0:
                return redirect(url_for('schedule', node=self.node,
                                        project=self.project, version=self.version))
            else:
                kwargs = dict(
                    node=self.node,
                    selected_nodes=self.selected_nodes,
                    first_selected_node=self.first_selected_node,
                    js=self.js,
                    project=self.project,
                    version=self.version,
                    url_projects_first_selected_node=url_for('projects', node=self.first_selected_node),
                    url_projects_list=[url_for('projects', node=n) for n in range(1, self.SCRAPYD_SERVERS_AMOUNT + 1)],
                    url_xhr=url_for('deploy.xhr', node=self.node, eggname=self.eggname,
                                    project=self.project, version=self.version),
                    url_schedule=url_for('schedule', node=self.node, project=self.project,
                                         version=self.version),
                    url_servers=url_for('servers', node=self.node, opt='schedule', project=self.project,
                                        version_job=self.version)
                )
                return render_template(self.template, **kwargs)

    def handle_form(self):
        # {'1': 'on',
        # '2': 'on',
        # 'checked_amount': '2',
        # 'folder': 'ScrapydWeb_demo',
        # 'project': 'demo',
        # 'version': '2018-09-05T03_13_50'}

        # With multinodes, would try to deploy to the first selected node first
        self.selected_nodes_amount = request.form.get('checked_amount', default=0, type=int)
        if self.selected_nodes_amount:
            self.selected_nodes = self.get_selected_nodes()
            self.first_selected_node = self.selected_nodes[0]
            self.url = 'http://{}/{}.json'.format(self.SCRAPYD_SERVERS[self.first_selected_node - 1], 'addversion')
            # Note that self.first_selected_node != self.node
            self.AUTH = self.SCRAPYD_SERVERS_AUTHS[self.first_selected_node - 1]
        else:
            self.url = 'http://{}/{}.json'.format(self.SCRAPYD_SERVER, 'addversion')

        # Error: Project names must begin with a letter and contain only letters, numbers and underscores
        self.project = re.sub(self.STRICT_NAME_PATTERN, '_', request.form.get('project', '')) or self.get_now_string()
        self.version = re.sub(self.LEGAL_NAME_PATTERN, '-', request.form.get('version', '')) or self.get_now_string()

        # Optional explicit candidate selection (relative path of the chosen scrapy.cfg) and,
        # for re-submission from the chooser, the archive already persisted in DEPLOY_PATH.
        self.scrapy_cfg_selected = request.form.get('scrapy_cfg', '')
        self.uploaded_filename = request.form.get('uploaded_filename', '')

        if request.files.get('file'):
            self.handle_uploaded_file()
        elif self.uploaded_filename:
            self.handle_uploaded_reselect()
        else:
            self.folder = request.form['folder']  # Used with SCRAPY_PROJECTS_DIR to get project_path
            self.handle_local_project()

    def handle_local_project(self):
        # Use folder instead of project
        project_path = os.path.join(self.SCRAPY_PROJECTS_DIR, self.folder)

        self.collect_scrapy_cfg_candidates(project_path)
        self.resolve_scrapy_cfg()
        # resolve_scrapy_cfg() flags scrapy_cfg_not_found / scrapy_cfg_multiple /
        # scrapy_cfg_invalid_selection when a single target cannot be built right away.
        if not self.scrapy_cfg_path:
            return

        self.finalize_eggname('%s_%s.egg' % (self.project, self.version), self.scrapy_cfg_chosen_rel)
        self.build_egg()

    def handle_uploaded_file(self):
        # http://flask.pocoo.org/docs/1.0/api/#flask.Request.form
        # <class 'werkzeug.datastructures.FileStorage'>
        file = request.files['file']

        # Non-ASCII would be omitted and resulting the filename as to 'egg' or 'tar.gz'
        filename = secure_filename(file.filename)
        # tar.xz only works on Linux and macOS
        if filename in ['egg', 'zip', 'tar.gz']:
            filename = '%s_%s.%s' % (self.project, self.version, filename)
        else:
            filename = '%s_%s_from_file_%s' % (self.project, self.version, filename)

        if filename.endswith('egg'):
            self.eggname = filename
            self.eggpath = os.path.join(self.DEPLOY_PATH, self.eggname)
            file.save(self.eggpath)
            self.scrapy_cfg_not_found = False
        else:  # Compressed file
            filepath = os.path.join(self.DEPLOY_PATH, filename)
            file.save(filepath)
            # Persist the archive name so the chooser can rebuild from it without re-uploading.
            self.uploaded_filename = filename
            self._process_archive(filepath, re.sub(r'(\.zip|\.tar\.gz)$', '.egg', filename))

    # https://gangmax.me/blog/2011/09/17/12-14-52-publish-532/
    # https://stackoverflow.com/a/49649784
    # When ScrapydWeb runs in Linux/macOS and tries to uncompress zip file from Windows_CN_cp936
    # UnicodeEncodeError: 'ascii' codec can't encode characters in position 7-8: ordinal not in range(128)
    # macOS + PY2 would raise OSError: Illegal byte sequence
    # Ubuntu + PY2 would raise UnicodeDecodeError in collect_scrapy_cfg_candidates() though f.extractall(tmpdir) works well
    def uncompress_to_tmpdir(self, filepath):
        self.logger.debug("Uncompressing %s", filepath)
        tmpdir = tempfile.mkdtemp(prefix="scrapydweb-uncompress-")
        if zipfile.is_zipfile(filepath):
            with zipfile.ZipFile(filepath, 'r') as f:
                if PY2:
                    tmpdir = tempfile.mkdtemp(prefix="scrapydweb-uncompress-")
                    for filename in f.namelist():
                        try:
                            filename_utf8 = filename.decode('gbk').encode('utf8')
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            filename_utf8 = filename
                        filepath_utf8 = os.path.join(tmpdir, filename_utf8)

                        try:
                            with io.open(filepath_utf8, 'wb') as f_utf8:
                                copyfileobj(f.open(filename), f_utf8)
                        except IOError:
                            # os.mkdir(filepath_utf8)
                            # zipfile from Windows "send to zipped" would meet the inner folder first:
                            # temp\\scrapydweb-uncompress-qrcyc0\\demo7/demo/'
                            mkdir_p(filepath_utf8)
                else:
                    f.extractall(tmpdir)
        else:  # tar.gz
            with tarfile.open(filepath, 'r') as tar:  # Open for reading with transparent compression (recommended).
                tar.extractall(tmpdir)

        self.logger.debug("Uncompressed to %s", tmpdir)
        # In case uploading a compressed file in which scrapy_cfg_dir contains none ascii in python 2,
        # whereas selecting a project for auto packaging, scrapy_cfg_dir is unicode
        # print(repr(tmpdir))
        # print(type(tmpdir))
        return tmpdir.decode('utf8') if PY2 else tmpdir

    def collect_scrapy_cfg_candidates(self, search_root, func_walk=os.walk, retry=True):
        # Walk the whole tree and collect every scrapy.cfg, so nested projects, backup dirs and
        # multiple candidate projects are all discoverable (the previous behaviour stopped at the
        # first scrapy.cfg encountered during the walk).
        self.scrapy_cfg_search_root = search_root
        try:
            for dirpath, dirnames, filenames in func_walk(search_root):
                self.scrapy_cfg_searched_paths.append(os.path.abspath(dirpath))
                scrapy_cfg = os.path.abspath(os.path.join(dirpath, 'scrapy.cfg'))
                if os.path.exists(scrapy_cfg):
                    rel = os.path.relpath(scrapy_cfg, search_root).replace('\\', '/')
                    project = ''
                    try:
                        project = get_config(scrapy_cfg).get('deploy', 'project')
                    except (ScrapyCfgParseError, UnicodeDecodeError) as err:
                        self.logger.debug("%s parse error for display: %s", scrapy_cfg, err)
                    project = project or os.path.basename(os.path.dirname(scrapy_cfg))
                    self.scrapy_cfg_candidates.append(dict(rel=rel, path=scrapy_cfg, project=project))
                    self.logger.debug("scrapy_cfg candidate: %s (project %s)", scrapy_cfg, project)
        except UnicodeDecodeError:
            msg = "Found illegal filenames in %s" % search_root
            self.logger.error(msg)
            flash(msg, self.WARN)
            if PY2 and retry:
                # Restart the walk with the unicode-safe variant; discard the partial results.
                self.scrapy_cfg_candidates = []
                self.scrapy_cfg_searched_paths = []
                self.collect_scrapy_cfg_candidates(search_root, func_walk=self.safe_walk, retry=False)
                return
            else:
                raise
        self.scrapy_cfg_candidates.sort(key=lambda c: c['rel'].lower())
        if not self.scrapy_cfg_candidates:
            self.logger.error("scrapy.cfg not found in: %s", search_root)

    def resolve_scrapy_cfg(self):
        # Decide which scrapy.cfg to build from the discovered candidates:
        #   0 candidates                      -> not found
        #   explicit selection given          -> build it (or flag an invalid selection)
        #   exactly 1 candidate, no selection -> build it (single-project auto-packaging)
        #   >1 candidates, no selection       -> let the caller choose (no build yet)
        candidates = self.scrapy_cfg_candidates
        if not candidates:
            self.scrapy_cfg_not_found = True
            return
        if self.scrapy_cfg_selected:
            selected = self.scrapy_cfg_selected.replace('\\', '/').strip()
            match = next((c for c in candidates if c['rel'] == selected), None)
            if match:
                self.scrapy_cfg_path = match['path']
                self.scrapy_cfg_chosen_rel = match['rel']
            else:
                self.scrapy_cfg_invalid_selection = True
            return
        if len(candidates) == 1:
            self.scrapy_cfg_path = candidates[0]['path']
            self.scrapy_cfg_chosen_rel = candidates[0]['rel']
            return
        self.scrapy_cfg_multiple = True

    def finalize_eggname(self, base_egg_name, chosen_rel):
        # Keep the canonical single-project name byte-identical. Only when more than one candidate
        # exists and a nested one is chosen do we append a sanitized sub-path, so the eggs of
        # different candidates never collide in DEPLOY_PATH.
        eggname = base_egg_name
        if len(self.scrapy_cfg_candidates) > 1:
            sub_dir = os.path.dirname(chosen_rel)
            if sub_dir:
                suffix = re.sub(self.STRICT_NAME_PATTERN, '_', sub_dir)
                eggname = '%s__%s.egg' % (base_egg_name[:-len('.egg')], suffix)
        self.eggname = eggname
        self.eggpath = os.path.join(self.DEPLOY_PATH, self.eggname)

    def _process_archive(self, filepath, base_egg_name):
        # Shared path for both the initial upload and the chooser re-submission: uncompress,
        # discover candidates, resolve a target and build it. The temp dir is always cleaned up
        # (the chooser re-submission rebuilds from the archive persisted in DEPLOY_PATH, so the
        # uncompressed tree is never needed across requests).
        tmpdir = self.uncompress_to_tmpdir(filepath)
        self.uncompress_tmpdir = tmpdir
        try:
            self.collect_scrapy_cfg_candidates(tmpdir)
            self.resolve_scrapy_cfg()
            if not self.scrapy_cfg_path:
                return
            self.finalize_eggname(base_egg_name, self.scrapy_cfg_chosen_rel)
            self.build_egg()
        finally:
            rmtree(tmpdir, ignore_errors=True)
            self.uncompress_tmpdir = ''

    def handle_uploaded_reselect(self):
        # Re-submission from the candidate chooser: the archive was saved to DEPLOY_PATH during
        # the initial upload, so rebuild from it instead of requiring the file to be re-uploaded.
        filename = secure_filename(self.uploaded_filename)
        filepath = os.path.join(self.DEPLOY_PATH, filename)
        if not filename or not os.path.exists(filepath):
            self.scrapy_cfg_not_found = True
            return
        self._process_archive(filepath, re.sub(r'(\.zip|\.tar\.gz)$', '.egg', filename))

    def build_egg(self):
        try:
            egg, tmpdir = _build_egg(self.scrapy_cfg_path)
        except ScrapyCfgParseError as err:
            self.logger.error(err)
            self.scrapy_cfg_parse_error = err
            return
        except CalledProcessError as err:
            self.logger.error(err)
            self.build_egg_subprocess_error = err
            return

        # Land the egg only in the canonical DEPLOY_PATH; do not scatter it into the scanned
        # source tree (which previously caused built eggs to mix with old artifacts there).
        copyfile(egg, self.eggpath)
        rmtree(tmpdir)
        self.logger.debug("Egg file saved to: %s", self.eggpath)

    def prepare_data(self):
        with io.open(self.eggpath, 'rb') as f:
            content = f.read()
            self.data = {
                'project': self.project,
                'version': self.version,
                'egg': content
            }

        self.slot.add_egg(self.eggname, content)


class DeployXhrView(BaseView):

    def __init__(self):
        super(DeployXhrView, self).__init__()

        self.eggname = self.view_args['eggname']
        self.project = self.view_args['project']
        self.version = self.view_args['version']

        self.url = 'http://{}/{}.json'.format(self.SCRAPYD_SERVER, 'addversion')

        self.slot = slot

    def dispatch_request(self, **kwargs):
        content = self.slot.egg.get(self.eggname)
        # content = None  # For test only
        if not content:
            eggpath = os.path.join(self.DEPLOY_PATH, self.eggname)
            with io.open(eggpath, 'rb') as f:
                content = f.read()

        data = {
            'project': self.project,
            'version': self.version,
            'egg': content
        }
        status_code, js = self.make_request(self.url, data=data, auth=self.AUTH)
        return self.json_dumps(js, as_response=True)
