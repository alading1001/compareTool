"""根目录恢复的真实端点，以及编辑任务时的配置快照回归。"""
import json
from unittest import mock

from archive_workflow_fixtures import Repository, WorkflowCase
from main import CompareToolApp
import test_review_configuration_and_report_semantics as config_tests
from test_review_five_integrity_fixes import delivered


class SVNRootRestoreTests(WorkflowCase):
    def restored_repository(self, name, *, ancestor=False, move=False):
        repo = Repository(self, 'svn', name)
        suffix = '/child' if ancestor else ''
        source = 'project' + suffix
        first = repo.commit({source + '/a.txt': b'original\n',
                             source + '/restored.txt': b'restored\n'})
        repo.cmd('delete', source + '/restored.txt')
        before = repo.commit({source + '/a.txt': b'updated\n',
                              source + '/extra.txt': b'added later\n'})
        target = 'destination' if move else 'project'
        repo.cmd('delete', 'project')
        repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / target))
        restored = repo.commit()
        return repo, target + suffix, before, restored

    def check_delivery(self, repo, scope, selected, name, old, new):
        task = repo.task(selected, '', multi=True, scope=repo.path / scope)
        app, result, out = self.generate(task, False, self.root / name)
        self.assert_success(app)
        self.assertEqual('completed', app._last_task_record['status'])
        self.assertFalse(result.issues)
        self.assertEqual(old, delivered(out, 'oldVersion'))
        self.assertEqual(new, delivered(out, 'newVersion'))
        self.assertEqual(len(result.report_manifest_files),
                         app._last_task_record['counts']['total_files'])
        self.assert_no_stages(out)
        return result, out

    def test_root_and_ancestor_restore_deliver_real_previous_revision(self):
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                repo, scope, before, restored = self.restored_repository(
                    str(ancestor), ancestor=ancestor)
                result, out = self.check_delivery(repo, scope, restored, 'out-' + str(ancestor),
                    {'Demo/a.txt': b'updated\n', 'Demo/extra.txt': b'added later\n'},
                    {'Demo/a.txt': b'original\n', 'Demo/restored.txt': b'restored\n'})
                self.assertEqual({('M', 'a.txt'), ('D', 'extra.txt'), ('A', 'restored.txt')},
                                 {(f.report_type, f.file_path) for f in result.report_manifest_files})
                instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
                self.assertIn('Demo/extra.txt', instructions)
                html = (out / 'report.html').read_text(encoding='utf-8')
                self.assertIn('updated', html)
                self.assertIn('original', html)

    def test_selected_history_before_restore_is_not_skipped_by_copyfrom(self):
        repo, scope, before, restored = self.restored_repository('history')
        self.check_delivery(repo, scope, before, 'before-only',
            {'Demo/a.txt': b'original\n', 'Demo/restored.txt': b'restored\n'},
            {'Demo/a.txt': b'updated\n', 'Demo/extra.txt': b'added later\n'})
        result, _ = self.check_delivery(repo, scope, before + ',' + restored, 'net-zero', {}, {})
        self.assertFalse(result.files)

    def test_stale_root_move_compares_actual_source_before_move(self):
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                repo, scope, before, restored = self.restored_repository(
                    str(ancestor), ancestor=ancestor, move=True)
                self.check_delivery(repo, scope, restored, 'out-' + str(ancestor),
                    {'Demo/a.txt': b'updated\n', 'Demo/extra.txt': b'added later\n'},
                    {'Demo/a.txt': b'original\n', 'Demo/restored.txt': b'restored\n'})
                self.check_delivery(repo, scope, before + ',' + restored,
                                    'net-zero-' + str(ancestor), {}, {})

    def test_restore_preserves_endpoint_properties_and_exclusions(self):
        repo = Repository(self, 'svn', 'properties')
        first = repo.commit({'project/a.txt': b'first\n', 'project/hidden.txt': b'first'})
        repo.cmd('propset', 'svn:eol-style', 'CRLF', 'project/a.txt')
        repo.commit({'project/a.txt': b'second\n', 'project/hidden.txt': b'second'})
        repo.cmd('delete', 'project')
        repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / 'project'))
        restored = repo.commit()
        task = repo.task(restored, '', multi=True, scope=repo.path / 'project')
        task['exclude_rules'] = 'hidden.txt'
        app, result, out = self.generate(task, False, self.root / 'out')
        self.assert_success(app)
        self.assertFalse(result.issues)
        self.assertEqual({'Demo/a.txt': b'second\r\n'}, delivered(out, 'oldVersion'))
        self.assertEqual({'Demo/a.txt': b'first\n'}, delivered(out, 'newVersion'))

    def test_stale_root_move_with_deleted_source_ancestor_ignores_overwritten_target(self):
        repo = Repository(self, 'svn', 'ancestor-delete')
        first = repo.commit({'container/project/a.txt': b'original',
                             'destination/unrelated.txt': b'other project'})
        before = repo.commit({'container/project/a.txt': b'updated',
                              'container/project/extra.txt': b'extra'})
        repo.cmd('delete', 'destination')
        repo.cmd('copy', repo.url + '/container/project@' + first,
                 str(repo.path / 'destination'))
        repo.cmd('delete', 'container')
        restored = repo.commit()
        self.check_delivery(repo, 'destination', restored, 'out',
            {'Demo/a.txt': b'updated', 'Demo/extra.txt': b'extra'},
            {'Demo/a.txt': b'original'})
        self.check_delivery(repo, 'destination', before + ',' + restored, 'net-zero', {}, {})

    def test_restore_with_child_rename_and_missing_predecessor(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                repo = Repository(self, 'svn', 'rename-' + str(missing))
                first = repo.commit({'project/a.txt': b'original', 'project/other.txt': b'first'})
                if missing:
                    repo.cmd('delete', 'project/a.txt')
                before = repo.commit({'project/other.txt': b'second'})
                repo.cmd('delete', 'project')
                repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / 'project'))
                repo.cmd('move', 'project/a.txt', 'project/renamed.txt')
                restored = repo.commit()
                old = {'Demo/other.txt': b'second'}
                if not missing:
                    old['Demo/a.txt'] = b'original'
                result, _ = self.check_delivery(repo, 'project', restored, 'out-' + str(missing),
                    old, {'Demo/other.txt': b'first', 'Demo/renamed.txt': b'original'})
                expected = ('A', 'renamed.txt', '') if missing else ('R', 'renamed.txt', 'a.txt')
                self.assertIn(expected, {(f.report_type, f.file_path, f.old_path)
                                         for f in result.report_manifest_files})
                # 删除被选中后再恢复，应沿已知身份合成 rename，而不是丢失旧端点。
                if missing:
                    result, _ = self.check_delivery(repo, 'project', before + ',' + restored,
                        'selected-delete', {'Demo/a.txt': b'original'},
                        {'Demo/renamed.txt': b'original'})
                    self.assertEqual([('R', 'renamed.txt', 'a.txt')],
                        [(f.report_type, f.file_path, f.old_path) for f in result.report_manifest_files])

    def test_repeated_restores_retain_noncontiguous_file_endpoints(self):
        repo, scope, before, restored = self.restored_repository('repeated')
        changed = repo.commit({scope + '/a.txt': b'fourth\n'})
        repo.cmd('delete', 'project')
        repo.cmd('copy', repo.url + '/project@' + before, str(repo.path / 'project'))
        final = repo.commit()
        self.check_delivery(repo, scope, changed, 'before-final',
            {'Demo/a.txt': b'original\n'}, {'Demo/a.txt': b'fourth\n'})
        self.check_delivery(repo, scope, before + ',' + final, 'noncontiguous',
            {'Demo/a.txt': b'original\n', 'Demo/restored.txt': b'restored\n'},
            {'Demo/a.txt': b'updated\n', 'Demo/extra.txt': b'added later\n'})


class MultiTaskEditSnapshotTests(WorkflowCase):
    def edit_task(self, task, key):
        config = dict(vcs_type='git', project_path='', multi_tasks=[task],
            output_dir=str(self.root / 'output'), project_exclude_rules={key: ''},
            project_display_options={key: dict(show_project_root=True, show_full_context=True,
                                               recursive_archives=False, ignore_archive_root=False)})
        with config_tests.ProjectExclusionRecoveryTests.configured_window(self, config) as (app, path, _):
            with mock.patch.object(app, '_selected_multi_task_index', return_value=0):
                app._edit_multi_task()
            app._refresh_output_paths_now()
            self.assertEqual(task['exclude_rules'], app._current_exclude_rules())
            self.assertEqual('no', app.show_project_root_var.get())
            self.assertEqual('no', app.show_full_context_var.get())
            self.assertTrue(app.recursive_archives_var.get())
            if task['vcs_type'] == 'archive':
                self.assertTrue(app.ignore_archive_root_var.get())
            app._add_or_update_multi_task()
            saved = json.loads(path.read_text(encoding='utf-8'))['multi_tasks'][0]
            self.assertEqual(key, saved['exclude_key'])
            self.assertEqual(task['exclude_rules'], saved['exclude_rules'])
            self.assertFalse(saved['show_project_root'])
            self.assertFalse(saved['show_full_context'])
            self.assertTrue(saved['recursive_archives'])
            return saved

    def test_all_modes_preserve_snapshot_with_missing_empty_or_stale_key(self):
        old, new = self.root / 'old', self.root / 'new'
        old.mkdir(); new.mkdir()
        old_archive, new_archive = self.root / 'old.zip', self.root / 'new.zip'
        old_archive.write_bytes(b'placeholder'); new_archive.write_bytes(b'placeholder')
        for mode in ('git', 'svn', 'git_multi', 'svn_multi', 'folder', 'archive'):
            for saved_key in (None, '', 'obsolete-location'):
                with self.subTest(mode=mode, saved_key=saved_key):
                    task = dict(vcs_type=mode, project_name='Demo', project_path=str(new),
                        old_version='1', new_version='2', exclude_rules='**/logs/**',
                        show_project_root=False, show_full_context=False,
                        recursive_archives=True, ignore_archive_root=True)
                    if mode == 'folder':
                        task.update(old_version=str(old), new_version=str(new))
                    if mode == 'archive':
                        task.update(old_version=str(old_archive), new_version=str(new_archive))
                    if saved_key is not None:
                        task['exclude_key'] = saved_key
                    key = CompareToolApp._normalize_project_path(
                        task['new_version'] if mode in ('folder', 'archive') else task['project_path'])
                    self.edit_task(task, key)

    def test_unchanged_edit_does_not_export_previously_excluded_files(self):
        old, new = self.root / 'old', self.root / 'new'
        for folder in (old, new):
            (folder / 'logs').mkdir(parents=True)
            (folder / 'a.txt').write_bytes(folder.name.encode())
            (folder / 'logs/x.txt').write_bytes(folder.name.encode())
        task = dict(vcs_type='folder', project_name='Demo', project_path='',
            old_version=str(old), new_version=str(new), exclude_rules='**/logs/**',
            show_project_root=False, show_full_context=False, recursive_archives=True)
        saved = self.edit_task(task, CompareToolApp._normalize_project_path(str(new)))
        app, result, out = self.generate(saved, False, self.root / 'out')
        self.assert_success(app)
        self.assertEqual(['a.txt'], [f.file_path for f in result.report_manifest_files])
        self.assertEqual({'Demo/a.txt': b'old'}, delivered(out, 'oldVersion'))
        self.assertEqual({'Demo/a.txt': b'new'}, delivered(out, 'newVersion'))
