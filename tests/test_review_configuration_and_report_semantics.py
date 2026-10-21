"""损坏排除配置、重命名展示路径及多版本报告的真实工作流回归。"""
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

import main
from archive_workflow_fixtures import Repository, WorkflowCase
from diff_engine import DiffResult
from report_generator import ReportGenerator


NODE = shutil.which('node')
SUMMARY_SCRIPT = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(0, 'utf8');
const pre = {textContent: ''};
const document = {open() {}, write() {}, close() {}, getElementById() {return pre;}};
const context = vm.createContext({window: {open() {return {document};}}});
// 清单复用独立权限数据，需同时执行报告中的共享清单函数。
const permissionHelper = html.lastIndexOf('function appendPermissionSummary(');
if (permissionHelper >= 0) {
    vm.runInContext(html.slice(permissionHelper, html.indexOf('var fileData = []', permissionHelper)), context);
}
vm.runInContext(html.slice(html.indexOf('var fileData = []'),
                          html.indexOf('var currentDiffAnchors')), context);
vm.runInContext(html.slice(html.indexOf('function displayPath('),
                          html.indexOf('function esc(')), context);
const start = html.indexOf('function openSummary()');
vm.runInContext(html.slice(start, html.indexOf('var filterState', start))
                + '\nopenSummary();', context);
process.stdout.write(JSON.stringify(pre.textContent));
"""


def summary_text(html):
    result = subprocess.run([NODE, '-e', SUMMARY_SCRIPT], input=html, text=True,
                            encoding='utf-8', capture_output=True, timeout=30, check=True)
    return json.loads(result.stdout)


class ProjectExclusionRecoveryTests(WorkflowCase):
    @contextmanager
    def configured_window(self, config):
        config_path = self.root/'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        with ExitStack() as stack:
            for name, value in [('CONFIG_DIR', str(self.root)), ('CONFIG_FILE', str(config_path)),
                                ('_CONFIG_LOAD_FAILURE', None)]:
                stack.enter_context(mock.patch.object(main, name, value))
            warning = stack.enter_context(mock.patch('main.warn'))
            app = main.CompareToolApp()
            app.root.withdraw()
            try:
                yield app, config_path, warning
            finally:
                for token in app.root.tk.call('after', 'info'):
                    app.root.after_cancel(token)
                app.root.destroy()

    def folder_config(self, rule):
        old, new = self.root/'before', self.root/'after'
        for directory in (old, new):
            for name in ('src/a.txt', 'logs/trace.txt', '.git/config'):
                path = directory/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(directory.name.encode())
        key = main.CompareToolApp._normalize_project_path(str(new))
        config = dict(vcs_type='folder', project_path='', output_dir=str(self.root/'output'),
            last_comparison_paths={'folder': {'old_version': str(old), 'new_version': str(new)}},
            project_exclude_rules={key: rule, 'other-project': '*.keep\n', 'explicit-empty': ''})
        return config, key, old, new

    def generate_from_window(self, app, old, new, output):
        task = dict(vcs_type='folder', project_name='Demo', project_path='',
                    old_version=str(old), new_version=str(new), exclude_rules=app._current_exclude_rules())
        worker, result, out = self.generate(task, False, output)
        self.assert_success(worker)
        self.assertFalse(result.delivery_incomplete, result.issues)
        for side, payload in (('oldVersion', b'before'), ('newVersion', b'after')):
            self.assertEqual(payload, (out/side/'Demo/src/a.txt').read_bytes())
        return result, out

    def test_non_string_project_rules_restore_defaults_before_export_and_save(self):
        for index, invalid in enumerate((None, [], {}, 42, True)):
            with self.subTest(value=invalid):
                config, key, old, new = self.folder_config(invalid)
                with self.configured_window(config) as (app, config_path, warning):
                    self.assertEqual(main.DEFAULT_EXCLUDE_RULES.strip(), app._current_exclude_rules())
                    result, out = self.generate_from_window(app, old, new, self.root/str(index))
                    self.assertEqual(['src/a.txt'], [f.file_path for f in result.files])
                    for side in ('oldVersion', 'newVersion'):
                        self.assertFalse((out/side/'Demo/logs').exists())
                        self.assertFalse((out/side/'Demo/.git').exists())
                    self.assertEqual(1, warning.call_count)
                    self.assertIn('排除规则', warning.call_args.args[0])
                    self.assertTrue(app._save_current_config())
                    saved = json.loads(config_path.read_text(encoding='utf-8'))['project_exclude_rules']
                    self.assertEqual(main.DEFAULT_EXCLUDE_RULES.strip(), saved[key])
                    self.assertEqual('*.keep\n', saved['other-project'])
                    self.assertEqual('', saved['explicit-empty'])

    def test_valid_empty_project_rules_still_disable_exclusions_and_survive_save(self):
        config, key, old, new = self.folder_config('')
        with self.configured_window(config) as (app, config_path, warning):
            self.assertEqual('', app._current_exclude_rules())
            result, out = self.generate_from_window(app, old, new, self.root/'empty-rules')
            self.assertEqual({'.git/config', 'logs/trace.txt', 'src/a.txt'},
                             {f.file_path for f in result.files})
            self.assertEqual(b'after', (out/'newVersion/Demo/logs/trace.txt').read_bytes())
            warning.assert_not_called()
            self.assertTrue(app._save_current_config())
            self.assertEqual('', json.loads(config_path.read_text(encoding='utf-8'))['project_exclude_rules'][key])


@unittest.skipUnless(NODE, 'Node.js is required to exercise the generated summary script')
class MultiProjectRenameDisplayTests(WorkflowCase):
    def task(self, name, old, new):
        paths = []
        for side, files in (('before', old), ('after', new)):
            directory = self.root/name/side
            directory.mkdir(parents=True)
            for path, payload in files.items():
                target = directory/path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            paths.append(str(directory))
        return dict(vcs_type='folder', project_name=name, project_path='', old_version=paths[0],
                    new_version=paths[1], exclude_rules='', show_project_root=False, show_full_context=True)

    def run_projects(self, tasks, output_name):
        out = self.root/output_name
        out.mkdir()
        captured = []
        original = ReportGenerator.generate_multi
        def observe(generator, projects, *args, **kwargs):
            captured.extend(projects)
            return original(generator, projects, *args, **kwargs)
        app = self.app()
        with mock.patch.object(ReportGenerator, 'generate_multi', observe):
            app._do_generate_multi(tasks, str(out/'report.html'), str(out/'oldVersion'),
                                   str(out/'newVersion'), str(out))
        self.assert_success(app)
        self.assertFalse(any(p['diff_result'].delivery_incomplete for p in captured))
        self.assertTrue(all(not task['show_project_root'] for task in tasks))
        self.assert_no_stages(out)
        return captured, out, summary_text((out/'report.html').read_text(encoding='utf-8'))

    def test_rename_source_conflicts_with_modified_path_and_other_rename_source(self):
        for renamed in (False, True):
            with self.subTest(other_is_rename=renamed):
                suffix = str(renamed)
                alpha, beta, independent = 'Alpha'+suffix, 'Beta'+suffix, 'Other'+suffix
                tasks = [self.task(alpha, {'config.txt': b'alpha'}, {'renamed.txt': b'alpha'}),
                    self.task(beta, {'config.txt': b'beta'},
                              {'beta-new.txt': b'beta'} if renamed else {'config.txt': b'beta changed'}),
                    self.task(independent, {'unique.txt': b'old'}, {'unique.txt': b'new'})]
                projects, out, summary = self.run_projects(tasks, 'collision'+suffix)
                self.assertEqual([True, True, False], [p['show_project_root'] for p in projects])
                self.assertIn(alpha+'/config.txt -> '+alpha+'/renamed.txt', summary)
                expected_beta = beta+'/config.txt' + (' -> '+beta+'/beta-new.txt' if renamed else '')
                self.assertIn('\n'+expected_beta+'\n', summary+'\n')
                self.assertIn('\nunique.txt\n', summary+'\n')
                self.assertEqual(b'alpha', (out/'oldVersion'/alpha/'config.txt').read_bytes())
                self.assertEqual(b'alpha', (out/'newVersion'/alpha/'renamed.txt').read_bytes())
                instructions = (out/'上线操作说明.txt').read_text(encoding='utf-8-sig')
                self.assertIn('[重命名旧路径] '+alpha+'/config.txt', instructions)
                self.assertTrue(all(any(i['stage'] == '展示路径' for i in p['diff_result'].issues)
                                    for p in projects[:2]))
                self.assertFalse(projects[2]['diff_result'].issues)

    def test_rename_old_path_is_rechecked_after_project_prefix_is_added(self):
        tasks = [self.task('Alpha', {'config.txt': b'a'}, {'renamed.txt': b'a'}),
                 self.task('Beta', {'config.txt': b'b'}, {'config.txt': b'new b'}),
                 self.task('Gamma', {'Alpha/config.txt': b'c'}, {'Alpha/config.txt': b'new c'})]
        projects, _out, summary = self.run_projects(tasks, 'transitive')
        self.assertEqual([True, True, True], [p['show_project_root'] for p in projects])
        self.assertIn('Alpha/config.txt -> Alpha/renamed.txt', summary)
        self.assertIn('\nGamma/Alpha/config.txt\n', summary+'\n')


class MultiVersionReportLabelsTests(WorkflowCase):
    def test_public_report_accepts_class_and_desktop_labels_but_preserves_normal_labels(self):
        for index, vcs_type in enumerate(('GitMultiVersionVCS', 'SVNMultiVersionVCS',
                'Git多版本', 'SVN多版本', 'GitVCS', 'SVNVCS', 'Git', 'SVN', '文件夹', '压缩包')):
            with self.subTest(vcs_type=vcs_type):
                result = DiffResult('demo', 'Demo', vcs_type, 'first', 'last')
                output = self.root/(str(index)+'.html')
                ReportGenerator().generate(result, str(output))
                html = output.read_text(encoding='utf-8')
                if index < 4:
                    self.assertIn('选中版本: first', html)
                    self.assertIn('生成结果: last', html)
                    self.assertNotIn('旧版本: first', html)
                    self.assertIn("var versionSummary = '选中版本: '", html)
                else:
                    self.assertIn('旧版本: first → 新版本: last', html)
                    self.assertNotIn('选中版本:', html)
                    self.assertIn("var versionSummary = oldVersion + ' -> ' + newVersion;", html)

    @unittest.skipUnless(NODE, 'Node.js is required to exercise the generated summary script')
    def test_git_and_svn_desktop_workers_render_selected_versions_and_export_fixed_bytes(self):
        for kind in ('git', 'svn'):
            with self.subTest(kind=kind):
                repo = Repository(self, kind, kind+'-labels')
                repo.commit({'a.txt': b'before\n'})
                first = repo.commit({'a.txt': b'after\n'})
                last = repo.commit({'b.txt': b'new\n'})
                selected = first+','+last
                app, result, out = self.generate(repo.task(selected, '', multi=True), False, self.root/(kind+'-out'))
                self.assert_success(app)
                self.assertFalse(result.delivery_incomplete, result.issues)
                html = (out/'report.html').read_text(encoding='utf-8')
                self.assertIn('选中版本: '+selected, html)
                self.assertIn('生成结果: 文件级首尾端点', html)
                self.assertNotIn('旧版本: '+selected, html)
                summary = summary_text(html)
                self.assertIn('选中版本: '+selected+' | 生成结果: 文件级首尾端点', summary)
                self.assertEqual(b'before\n', (out/'oldVersion/Demo/a.txt').read_bytes())
                self.assertEqual(b'after\n', (out/'newVersion/Demo/a.txt').read_bytes())
                self.assertEqual(b'new\n', (out/'newVersion/Demo/b.txt').read_bytes())
                self.assert_no_stages(out)
