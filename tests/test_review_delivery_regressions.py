"""复核三个审查问题：双向替换、成对归档回退和损坏配置启动。"""
from contextlib import ExitStack
import json
from pathlib import Path
from unittest import mock

import main
import test_best_effort_export as best_effort
from archive_workflow_fixtures import WorkflowCase, Repository
from file_exporter import FileExporter
from test_archive_comparison_root import write_archive
from vcs.archive_vcs import ArchiveVCS


class ReplacementDeliveryTests(WorkflowCase):
    commit_index_tree = best_effort.BestEffortWorkflowTests.commit_index_tree

    def assert_independent_delivery(self, app, result, out, extras=()):
        self.assert_success(app)
        self.assertTrue(result.delivery_incomplete)
        self.assertEqual('partial', app._last_task_record['status'])
        self.assertEqual({'good.txt', *extras}, {f.file_path for f in result.files})
        self.assertEqual([], result.required_directory_deletions)
        for side, payload in (('oldVersion', b'old good'), ('newVersion', b'new good')):
            self.assertEqual(payload, (out/side/'Demo/good.txt').read_bytes())
            paths = {p.relative_to(out/side/'Demo').as_posix()
                     for p in (out/side/'Demo').rglob('*') if p.is_file()}
            self.assertTrue(paths <= {'good.txt', *extras}, paths)
        instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
        self.assertNotIn('[删除文件] Demo/Node', instructions)
        self.assertNotIn('[删除旧目录后再写入同名文件] Demo/Node', instructions)
        self.assertIn('未完成', (out/'report.html').read_text(encoding='utf-8'))
        self.assertTrue(any(i['stage'] == '目录替换依赖' for i in result.issues))
        self.assert_no_stages(out)

    def test_git_only_failed_child_still_blocks_both_replacement_directions(self):
        for directory_to_file in (True, False):
            repo = Repository(self, 'git', f'special{directory_to_file}')
            old_node = {'Node/link': ('120000', b'elsewhere')} if directory_to_file else {
                'Node': ('100644', b'original important content')}
            new_node = {'Node': ('100644', b'replacement')} if directory_to_file else {
                'Node/link': ('120000', b'elsewhere')}
            old = self.commit_index_tree(repo, dict(old_node, **{'good.txt': ('100644', b'old good')}))
            new = self.commit_index_tree(repo, dict(new_node, **{'good.txt': ('100644', b'new good')}), old)
            for multi in (False, True):
                with self.subTest(directory_to_file=directory_to_file, multi=multi):
                    self.assert_independent_delivery(*self.generate(
                        repo.task(new if multi else old, new, multi=multi), False,
                        self.root/f'special-output-{directory_to_file}-{multi}'))

    def test_git_required_filter_blocks_replacement_on_either_endpoint(self):
        for directory_to_file in (True, False):
            for failed_side in ('old', 'new'):
                repo = Repository(self, 'git', f'filter{directory_to_file}{failed_side}')
                repo.cmd('config', 'filter.unavailable.required', 'true')
                old_path, new_path = (('Node/child.txt', 'Node') if directory_to_file
                                      else ('Node', 'Node/child.txt'))
                attributes = ((old_path if failed_side == 'old' else new_path)
                              + ' filter=unavailable\n').encode()
                old = self.commit_index_tree(repo, {old_path: ('100644', b'original important content'),
                    '.gitattributes': ('100644', attributes), 'good.txt': ('100644', b'old good')})
                new = self.commit_index_tree(repo, {new_path: ('100644', b'completely different replacement'),
                    '.gitattributes': ('100644', attributes), 'good.txt': ('100644', b'new good')}, old)
                for multi in (False, True):
                    with self.subTest(direction=directory_to_file, side=failed_side, multi=multi):
                        self.assert_independent_delivery(*self.generate(
                            repo.task(new if multi else old, new, multi=multi), False,
                            self.root/f'filter-output-{directory_to_file}-{failed_side}-{multi}'))

    def test_svn_keywords_failure_blocks_both_replacement_directions(self):
        for directory_to_file in (True, False):
            for failed_side in ('old', 'new'):
                repo = Repository(self, 'svn', f'keywords{directory_to_file}{failed_side}')
                old_path, new_path = (('Node/child.txt', 'Node') if directory_to_file
                                      else ('Node', 'Node/child.txt'))
                old = repo.commit({old_path: b'original important content', 'good.txt': b'old good'})
                if failed_side == 'old':
                    repo.cmd('propset', 'svn:keywords', 'Id', old_path)
                    old = repo.commit()
                repo.cmd('delete', 'Node')
                repo.write({new_path: b'completely different replacement', 'good.txt': b'new good'})
                repo.cmd('add', '--force', '.')
                if failed_side == 'new':
                    repo.cmd('propset', 'svn:keywords', 'Id', new_path)
                new = repo.commit()
                for multi in (False, True):
                    with self.subTest(direction=directory_to_file, side=failed_side, multi=multi):
                        self.assert_independent_delivery(*self.generate(
                            repo.task(new if multi else old, new, multi=multi), False,
                            self.root/f'svn-output-{directory_to_file}-{failed_side}-{multi}'))

    def test_folder_and_archive_write_failure_blocks_both_directions(self):
        original = FileExporter._write_file
        for kind in ('folder', 'archive'):
            for directory_to_file in (True, False):
                old_path, new_path = (('Node/child.txt', 'node') if directory_to_file
                                      else ('Node', 'node/child.txt'))
                endpoints = []
                for side, files in (('old', {old_path: b'old node', 'good.txt': b'old good'}),
                                    ('new', {new_path: b'new replacement', 'good.txt': b'new good'})):
                    target = self.root/f'{kind}-{directory_to_file}-{side}'
                    if kind == 'archive':
                        target = target.with_suffix('.zip'); write_archive(target, files)
                    else:
                        for path, payload in files.items():
                            file = target/path; file.parent.mkdir(parents=True, exist_ok=True)
                            file.write_bytes(payload)
                    endpoints.append(str(target))
                task = dict(vcs_type=kind, project_name='Demo', old_version=endpoints[0],
                            new_version=endpoints[1], exclude_rules='')
                for index, failed_path in enumerate((old_path, new_path)):
                    with self.subTest(kind=kind, direction=directory_to_file, failed=failed_path):
                        def fail(exporter, base, path, version, content):
                            if path == failed_path:
                                raise OSError('injected endpoint write failure')
                            return original(exporter, base, path, version, content)
                        with mock.patch.object(FileExporter, '_write_file', fail):
                            self.assert_independent_delivery(*self.generate(task, False,
                                self.root/f'write-output-{kind}-{directory_to_file}-{index}'))


class ArchiveFallbackTests(WorkflowCase):
    def archives(self, suffix, old, new):
        paths = self.root/('old'+suffix), self.root/('new'+suffix)
        for path, entries in zip(paths, (old, new)):
            write_archive(path, entries)
        return paths

    def test_fallback_freezes_both_exclusion_plans_at_full_package_root(self):
        for suffix in ('.zip', '.tar'):
            for extra_side in ('old', 'new'):
                with self.subTest(suffix=suffix, extra_side=extra_side):
                    entries = {side: {'root/logs/keep.txt': side.encode(), 'root/drop.txt': side.encode()}
                               for side in ('old', 'new')}
                    entries[extra_side]['top.txt'] = b'top-level'
                    paths = self.archives(suffix, entries['old'], entries['new'])
                    task = dict(vcs_type='archive', project_name='Demo', old_version=str(paths[0]),
                        new_version=str(paths[1]), ignore_archive_root=True,
                        exclude_rules='logs/**\nroot/drop.txt')
                    app, result, out = self.generate(task, False, self.root/f'out-{suffix}-{extra_side}')
                    self.assert_success(app)
                    self.assertFalse(result.delivery_incomplete, result.issues)
                    self.assertIn('已按完整包路径比较', result.comparison_note)
                    self.assertEqual({'root/logs/keep.txt', 'top.txt'}, {f.file_path for f in result.files})
                    for side, version in (('old', 'oldVersion'), ('new', 'newVersion')):
                        self.assertEqual(side.encode(), (out/version/'Demo/root/logs/keep.txt').read_bytes())
                        self.assertFalse((out/version/'Demo/root/drop.txt').exists())
                    self.assertIn('已按完整包路径比较', (out/'report.html').read_text(encoding='utf-8'))
                    self.assertIn('已按完整包路径比较', next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig'))
                    self.assert_no_stages(out)

    def test_strict_api_still_rejects_inapplicable_root_with_exclusions(self):
        for suffix in ('.zip', '.tar'):
            paths = self.archives(suffix, {'root/a.txt': b'old'}, {'root/a.txt': b'new', 'top.txt': b'top'})
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, '最外层单一文件夹'):
                ArchiveVCS(*map(str, paths), ignore_single_root=True, extraction_excludes=['logs/**'])

    def test_valid_paired_roots_keep_inner_exclusion_semantics(self):
        for suffix in ('.zip', '.tar'):
            paths = self.archives(suffix, {'v1/a.txt': b'old', 'v1/logs/drop.txt': b'old'},
                                  {'v2/a.txt': b'new', 'v2/logs/drop.txt': b'new'})
            vcs = ArchiveVCS(*map(str, paths), ignore_single_root=True,
                             extraction_excludes=['logs/**'], best_effort=True)
            try:
                self.assertEqual(['a.txt'], [f.path for f in vcs.get_changed_files()])
                self.assertEqual(b'old', vcs.get_file_content_raw_bytes(str(paths[0]), 'a.txt'))
                self.assertEqual(b'new', vcs.get_file_content_raw_bytes(str(paths[1]), 'a.txt'))
                self.assertEqual(('v1', 'v2'), (vcs.old_root_prefix, vcs.new_root_prefix))
            finally:
                vcs.cleanup()

    def test_trailing_names_are_validated_against_final_paired_root(self):
        for suffix in ('.zip', '.tar'):
            paths = self.archives(suffix, {'root/a.txt': b'old', 'root/logs/trailing.': b'old'},
                {'root/a.txt': b'new', 'root/logs/trailing.': b'new', 'top.txt': b'top'})
            vcs = ArchiveVCS(*map(str, paths), ignore_single_root=True,
                extraction_excludes=['root/logs/**'], best_effort=True)
            try:
                self.assertEqual({'root/a.txt', 'top.txt'}, {f.path for f in vcs.get_changed_files()})
                self.assertFalse((Path(vcs._tmp_old)/'root/logs/trailing.').exists())
            finally:
                vcs.cleanup()
            with self.subTest(suffix=suffix), self.assertRaises((ValueError, RuntimeError)):
                ArchiveVCS(*map(str, paths), ignore_single_root=True,
                           extraction_excludes=['logs/**'], best_effort=True)

    def test_fallback_does_not_suppress_path_or_crc_failures(self):
        for suffix in ('.zip', '.tar'):
            paths = self.archives(suffix, {'root/a.txt': b'old'},
                                  {'root/a.txt': b'new', '../escape.txt': b'unsafe'})
            with self.subTest(suffix=suffix), self.assertRaises((ValueError, RuntimeError)):
                ArchiveVCS(*map(str, paths), ignore_single_root=True,
                           extraction_excludes=['**'], best_effort=True)
        paths = self.archives('.zip', {'root/a.txt': b'old'},
                              {'root/a.txt': b'payload to corrupt', 'top.txt': b'top'})
        paths[1].write_bytes(paths[1].read_bytes().replace(b'payload to corrupt', b'corrupt to payload'))
        import zipfile
        with self.assertRaises((zipfile.BadZipFile, ValueError, RuntimeError)):
            ArchiveVCS(*map(str, paths), ignore_single_root=True,
                       extraction_excludes=['**'], best_effort=True)


class MalformedConfigurationTests(WorkflowCase):
    def test_real_window_skips_bad_records_and_preserves_valid_records_after_save(self):
        project = self.root/'project'; project.mkdir()
        valid_task = dict(vcs_type='git_multi', project_name='Valid', project_path=str(project),
                          old_version='abc123', new_version='legacy')
        valid_recent = dict(vcs_family='git', project_path=str(project), project_name='Valid')
        invalid_values = [[], {}, 42, True, None]
        config = dict(vcs_type=[], output_dir=str(self.root/'output'),
            multi_tasks=[dict(valid_task, vcs_type=v) for v in invalid_values] + [valid_task],
            recent_projects=[dict(valid_recent, **{field: value})
                             for field in ('project_path', 'project_name', 'vcs_family')
                             for value in [[], {}, 42, True]] + [dict(valid_recent, last_used={})])
        config_path = self.root/'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        with ExitStack() as stack:
            stack.enter_context(mock.patch('main.CONFIG_DIR', str(self.root)))
            stack.enter_context(mock.patch('main.CONFIG_FILE', str(config_path)))
            stack.enter_context(mock.patch('main._CONFIG_LOAD_FAILURE', None))
            stack.enter_context(mock.patch('main.messagebox.askyesno', return_value=False))
            warning = stack.enter_context(mock.patch('main.warn'))
            app = main.CompareToolApp(); app.root.withdraw()
            def dispose():
                for token in app.root.tk.call('after', 'info'):
                    app.root.after_cancel(token)
                app.root.destroy()
            stack.callback(dispose)
            self.assertEqual('git', app.vcs_var.get())
            self.assertEqual(['Valid'], [t['project_name'] for t in app._multi_tasks])
            self.assertEqual('文件级首尾端点', app._multi_tasks[0]['new_version'])
            self.assertEqual([dict(valid_recent, last_used='')], app._recent_projects)
            self.assertGreaterEqual(warning.call_count, 18)
            self.assertTrue(app._save_current_config())
            saved = json.loads(config_path.read_text(encoding='utf-8'))
            self.assertEqual(app._multi_tasks, saved['multi_tasks'])
            self.assertEqual(app._recent_projects, saved['recent_projects'])

    def test_task_type_validation_accepts_all_supported_modes_around_bad_values(self):
        app = self.app()
        tasks = [dict(vcs_type=kind, project_name=kind, project_path=str(self.root),
                      old_version='1', new_version='2') for kind in sorted(main.SUPPORTED_VCS_TYPES)]
        invalid = [dict(tasks[0], vcs_type=value) for value in ([], {}, None, 42, True, 'unknown')]
        with mock.patch('main.warn') as warning:
            normalized = app._normalize_loaded_multi_tasks(invalid[:3] + tasks + invalid[3:])
        self.assertEqual(main.SUPPORTED_VCS_TYPES, {t['vcs_type'] for t in normalized})
        self.assertEqual(6, warning.call_count)
