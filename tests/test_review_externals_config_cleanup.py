"""Review regressions: unchanged externals, damaged display options and stale caches."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time
import tkinter as tk
from unittest import mock

import main
from archive_report import _empty_archive
from archive_workflow_fixtures import Repository, WorkflowCase
from vcs.multi_version_vcs import SVNMultiVersionVCS
from vcs.raw_cache import RawEndpointCache
from vcs.svn_vcs import SVNVCS
from vcs import temp_storage


class UnchangedExternalsTests(WorkflowCase):
    def repository(self, name, *, owner='', definition='^/vendor ext', main_change=True):
        repo = Repository(self, 'svn', name)
        repo.write({'app/a.txt': b'old\n', 'vendor/lib.txt': b'old library\n',
                    'app/dependencies/keep.txt': b'unchanged\n'})
        repo.cmd('add', 'app', 'vendor')
        repo.cmd('propset', 'svn:externals', definition, 'app/' + owner)
        repo.cmd('commit', '-m', 'existing external definition')
        repo.cmd('update', '--ignore-externals')
        old = repo.cmd('info', '--show-item', 'revision')
        repo.write({'vendor/lib.txt': b'new library\n'})
        if main_change:
            repo.write({'app/a.txt': b'new\n'})
        repo.cmd('commit', '-m', 'files only')
        repo.cmd('update', '--ignore-externals')
        new = repo.cmd('info', '--show-item', 'revision')
        return repo, old, new

    def assert_rejected(self, repo, old, new, scope=None):
        scope = scope or repo.path/'app'
        normal = SVNVCS(str(scope))
        self.addCleanup(normal.cleanup)
        with self.assertRaisesRegex(RuntimeError, 'svn:externals'):
            normal.get_changed_files(old, new)
        with self.assertRaisesRegex(RuntimeError, 'svn:externals'):
            SVNMultiVersionVCS(str(scope), [new])
        for multi in (False, True):
            with self.subTest(multi=multi):
                task = repo.task(new if multi else old, new, multi=multi, scope=scope)
                output = self.root/(repo.base.name + str(multi))
                app, result, output = self.generate(task, False, output)
                self.assert_success(app)
                self.assertEqual('partial', app._last_task_record['status'])
                self.assertTrue(result.delivery_incomplete)
                self.assertEqual([], result.files)
                self.assertTrue(any('svn:externals' in issue['message'] for issue in result.issues))
                self.assertEqual({'oldVersion': {}, 'newVersion': {}}, self.delivery(output))
                self.assertIn('svn:externals', (output/'report.html').read_text(encoding='utf-8'))
                instructions = next(output.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
                self.assertIn('svn:externals', instructions)
                self.assert_no_stages(output)
        self.assertFalse(list((self.root/'runtime').glob('comparetool_svn_multi_*')))

    def test_unchanged_root_and_child_definitions_are_rejected_in_both_modes(self):
        for owner in ('', 'dependencies'):
            with self.subTest(owner=owner):
                self.assert_rejected(*self.repository('root' if not owner else 'child', owner=owner))

    def test_external_only_change_cannot_report_zero_change_success(self):
        self.assert_rejected(*self.repository('only-external', main_change=False))

    def test_fixed_historical_root_is_used_after_later_project_move(self):
        repo, old, new = self.repository('moved')
        renamed = 'project %20@root'
        repo.cmd('move', 'app', renamed)
        repo.cmd('commit', '-m', 'later root move')
        repo.cmd('update', '--ignore-externals')
        self.assertTrue((repo.path/renamed).is_dir())
        self.assert_rejected(repo, old, new, repo.path/renamed)
        current = SVNVCS(str(repo.path/renamed))
        current._pin_source_identity()
        with self.assertRaisesRegex(RuntimeError, '启用了 svn:externals'):
            current._validate_project_externals(current._pinned_peg_revision, current._pinned_project_url)

    def test_switch_after_identity_pin_does_not_hide_original_externals(self):
        for multi in (False, True):
            repo, old, new = self.repository('switch' + str(multi))
            repo.write({'clean/a.txt': b'no external\n'})
            repo.cmd('add', 'clean'); repo.cmd('commit', '-m', 'alternative project')
            repo.cmd('update', '--ignore-externals')
            scope = repo.path/'app'
            vcs_class = SVNMultiVersionVCS if multi else SVNVCS
            original = vcs_class._run
            switched = []
            def query(vcs, args):
                output = original(vcs, args)
                if args == ['info', '--xml', '--non-interactive', '-r', 'HEAD', '.'] and not switched:
                    switched.append(True)
                    repo.cmd('switch', '--ignore-ancestry', '--ignore-externals', repo.url + '/clean', str(scope))
                return output
            with self.subTest(multi=multi), mock.patch.object(vcs_class, '_run', query):
                with self.assertRaisesRegex(RuntimeError, 'svn:externals'):
                    if multi:
                        SVNMultiVersionVCS(str(scope), [new])
                    else:
                        normal = SVNVCS(str(scope)); self.addCleanup(normal.cleanup)
                        normal.get_changed_files(old, new)
            self.assertEqual([True], switched)

    def test_mixed_report_keeps_independent_project_when_both_svn_modes_fail(self):
        repo, old, new = self.repository('mixed')
        old_dir, new_dir = self.root/'old', self.root/'new'
        old_dir.mkdir(); new_dir.mkdir()
        (old_dir/'ok.txt').write_bytes(b'old\n'); (new_dir/'ok.txt').write_bytes(b'new\n')
        tasks = [repo.task(new if multi else old, new, multi=multi, scope=repo.path/'app',
                           project_name='MultiSVN' if multi else 'SVN') for multi in (False, True)]
        tasks.append(dict(vcs_type='folder', project_name='Healthy', project_path='',
                          old_version=str(old_dir), new_version=str(new_dir), exclude_rules=''))
        output = self.root/'mixed-output'; output.mkdir()
        app = self.app()
        app._do_generate_multi(tasks, str(output/'report.html'), str(output/'oldVersion'),
                               str(output/'newVersion'), str(output))
        self.assert_success(app)
        self.assertEqual('partial', app._last_task_record['status'])
        self.assertEqual(b'new\n', (output/'newVersion/Healthy/ok.txt').read_bytes())
        self.assertEqual({'Healthy/ok.txt'}, set(self.delivery(output)['newVersion']))
        self.assertIn('svn:externals', (output/'report.html').read_text(encoding='utf-8'))
        self.assert_no_stages(output)

    def test_empty_comments_and_excluded_trees_still_export(self):
        cases = [('', '', []), ('', ' # disabled\n\t# still disabled\n', []),
                 ('dependencies', '^/vendor ext', ['dependencies/**'])]
        for number, (owner, definition, excludes) in enumerate(cases):
            repo, old, new = self.repository('allowed' + str(number), owner=owner, definition=definition)
            for multi in (False, True):
                with self.subTest(case=number, multi=multi):
                    task = repo.task(new if multi else old, new, multi=multi, scope=repo.path/'app')
                    task['exclude_rules'] = '\n'.join(excludes)
                    app, result, out = self.generate(task, False, self.root/f'allowed-out-{number}-{multi}')
                    self.assert_success(app)
                    self.assertEqual('completed', app._last_task_record['status'])
                    self.assertFalse(result.delivery_incomplete)
                    self.assertEqual(['a.txt'], [f.file_path for f in result.files])
                    self.assertEqual(b'old\n', (out/'oldVersion/Demo/a.txt').read_bytes())
                    self.assertEqual(b'new\n', (out/'newVersion/Demo/a.txt').read_bytes())

    def test_multiversion_checks_selected_middle_revision_not_only_outer_bounds(self):
        repo, _old, selected = self.repository('gap', definition='')
        repo.cmd('propset', 'svn:externals', '^/vendor ext', 'app/dependencies')
        repo.write({'app/a.txt': b'middle\n'})
        repo.cmd('commit', '-m', 'enable external in selected middle revision')
        repo.cmd('update', '--ignore-externals')
        middle = repo.cmd('info', '--show-item', 'revision')
        repo.cmd('propdel', 'svn:externals', 'app/dependencies')
        repo.cmd('commit', '-m', 'unselected removal')
        repo.cmd('update', '--ignore-externals')
        repo.write({'app/a.txt': b'last\n'})
        repo.cmd('commit', '-m', 'last selected change')
        repo.cmd('update', '--ignore-externals')
        last = repo.cmd('info', '--show-item', 'revision')
        with self.assertRaisesRegex(RuntimeError, 'svn:externals'):
            SVNMultiVersionVCS(str(repo.path/'app'), [selected, middle, last])

    def test_first_project_creation_has_no_old_root_to_validate(self):
        repo, old, _new = self.repository('created', definition='# disabled')
        vcs = SVNMultiVersionVCS(str(repo.path/'app'), [old])
        self.addCleanup(vcs.cleanup)
        self.assertEqual({'a.txt', 'dependencies/keep.txt'}, {f.path for f in vcs.get_changed_files()})

    def test_property_query_and_xml_failures_are_not_silently_accepted(self):
        vcs = SVNVCS(str(self.root))
        def command(*_args, **kwargs):
            kwargs['stdout'].write(b'<properties><target')
            return mock.Mock(returncode=0, stderr=b'')
        with mock.patch('vcs.svn_vcs.subprocess.run', side_effect=command):
            with self.assertRaisesRegex(RuntimeError, '无法解析 SVN 项目 externals'):
                vcs._validate_project_externals('1', 'https://example.invalid/app')
        with mock.patch('vcs.svn_vcs.subprocess.run', return_value=mock.Mock(
                returncode=1, stderr=b'authentication failed')):
            with self.assertRaisesRegex(RuntimeError, 'authentication failed'):
                vcs._validate_project_externals('1', 'https://example.invalid/app')


class DisplayOptionsRecoveryTests(WorkflowCase):
    def test_real_window_ignores_bad_project_options_and_preserves_valid_settings(self):
        project = self.root/'project'; project.mkdir()
        other = self.root/'other'; other.mkdir()
        key = main.CompareToolApp._normalize_project_path(str(project))
        other_key = main.CompareToolApp._normalize_project_path(str(other))
        valid = dict(recursive_archives=True, show_project_root=False, show_full_context=False,
                     ignore_archive_root=True)
        config_path = self.root/'config.json'
        invalid = [['bad'], 'bad', 42, True, None, []]
        for value in invalid:
            config = dict(vcs_type='git', project_path=str(project), output_dir=str(self.root/'out'),
                          project_display_options={key: value, other_key: valid, 'unused': ['bad']})
            config_path.write_text(json.dumps(config), encoding='utf-8')
            with self.subTest(value=value), ExitStack() as stack:
                for name, setting in [('CONFIG_DIR', str(self.root)), ('CONFIG_FILE', str(config_path)),
                                      ('_CONFIG_LOAD_FAILURE', None)]:
                    stack.enter_context(mock.patch.object(main, name, setting))
                warning = stack.enter_context(mock.patch('main.warn'))
                # Clean up even if construction fails, so a regression does not leak Tk roots.
                def dispose():
                    if tk._default_root is not None:
                        for token in tk._default_root.tk.call('after', 'info'):
                            tk._default_root.after_cancel(token)
                        tk._default_root.destroy()
                stack.callback(dispose)
                app = main.CompareToolApp(); app.root.withdraw()
                self.assertFalse(app.recursive_archives_var.get())
                self.assertEqual('yes', app.show_project_root_var.get())
                self.assertEqual('yes', app.show_full_context_var.get())
                self.assertNotIn('unused', app._project_display_options)
                self.assertEqual(valid, app._project_display_options[other_key])
                self.assertEqual(2, warning.call_count)
                app.dir_entry.delete(0, tk.END); app.dir_entry.insert(0, str(other))
                app._switch_exclude_rules_for_current_source()
                self.assertTrue(app.recursive_archives_var.get())
                self.assertEqual('no', app.show_full_context_var.get())
                self.assertEqual('no', app.show_project_root_var.get())
                self.assertTrue(app._save_current_config())
                saved = json.loads(config_path.read_text(encoding='utf-8'))['project_display_options']
                self.assertEqual(dict(valid, show_project_root='no', show_full_context='no'), saved[other_key])
                self.assertTrue(all(isinstance(options, dict) for options in saved.values()))


class StaleCacheRecoveryTests(WorkflowCase):
    def test_owned_raw_and_empty_archive_caches_follow_age_and_owner_rules(self):
        with ExitStack() as stack:
            cache = RawEndpointCache(); stack.callback(cache.close)
            empty = Path(stack.enter_context(_empty_archive())).parent
            runtime = Path(cache.root).parent
            expected = {Path(cache.root), empty}
            for path in expected:
                marker = Path(str(path) + temp_storage._TEMP_MARKER_SUFFIX)
                payload = json.loads(marker.read_text(encoding='utf-8'))
                payload.update(created=time.time() - 9*24*3600, pid=999999)
                marker.write_text(json.dumps(payload), encoding='utf-8')
            for label, magic, age, pid in [('live', temp_storage._TEMP_MAGIC, 9, os.getpid()),
                                           ('young', temp_storage._TEMP_MAGIC, 1, 999999),
                                           ('invalid', 'untrusted', 9, 999999)]:
                path = runtime/('comparetool_svn_raw_' + label); path.mkdir()
                Path(str(path) + temp_storage._TEMP_MARKER_SUFFIX).write_text(json.dumps(
                    dict(magic=magic, created=time.time()-age*24*3600, pid=pid)), encoding='utf-8')
            (runtime/'comparetool_svn_raw_unowned').mkdir()
            unknown = runtime/'unrelated'; unknown.mkdir()
            Path(str(unknown) + temp_storage._TEMP_MARKER_SUFFIX).write_text(json.dumps(dict(
                magic=temp_storage._TEMP_MAGIC, created=time.time()-9*24*3600, pid=999999)))
            with mock.patch.object(temp_storage, '_CLEANED_ROOTS', set()), \
                    mock.patch.object(temp_storage, 'pid_is_alive', side_effect=lambda pid: pid == os.getpid()), \
                    mock.patch.object(temp_storage, 'remove_temp_dir') as remove:
                temp_storage._cleanup_stale_temp_dirs(str(runtime))
                self.assertEqual(expected, {Path(call.args[0]) for call in remove.call_args_list})
