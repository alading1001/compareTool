"""替换引用下的真实检出字节，以及归档改名时的权限说明。"""
import os
import subprocess
from unittest import mock

from archive_workflow_fixtures import Repository, WorkflowCase
from test_archive_comparison_root import write_archive
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS


class GitReplaceRefTests(WorkflowCase):
    def repository(self, name, kind='commit', prefix='refs/replace/'):
        repo = Repository(self, 'git', name)
        old = repo.commit({'.gitattributes': b'*.txt text eol=lf\n',
                           'a.txt': b'old\n', 'good.py': b'old good\n'})
        original = repo.commit({'a.txt': b'new\n', 'good.py': b'new good\n'})
        candidate = repo.commit({'.gitattributes': b'*.txt text eol=crlf\n',
                                 'a.txt': b'replaced\n'})
        tree = repo.cmd('rev-parse', candidate + '^{tree}')
        replacement = repo.cmd('commit-tree', tree, '-p', old, '-m', 'replacement sibling')
        repo.cmd('update-ref', 'HEAD', original)
        if kind == 'commit':
            pairs = [(original, replacement)]
        elif kind == 'tree':
            pairs = [(repo.cmd('rev-parse', original + '^{tree}'), tree)]
        else:
            pairs = [(repo.cmd('rev-parse', original + ':' + path),
                      repo.cmd('rev-parse', replacement + ':' + path))
                     for path in ('.gitattributes', 'a.txt')]
        for before, after in pairs:
            repo.cmd('update-ref', prefix + before, after)
        return repo, old, original, replacement

    def native(self, repo, revision, path='a.txt'):
        return subprocess.check_output(
            [repo.executable, 'cat-file', '--filters', '--path=' + path, revision + ':' + path],
            cwd=repo.path, env=dict(os.environ, GIT_ATTR_SOURCE=revision))

    def check_workers(self, repo, old, new, name):
        before, after = self.native(repo, old), self.native(repo, new)
        for multi in (False, True):
            with self.subTest(multi=multi):
                task = repo.task(new if multi else old, new, multi=multi)
                app, result, out = self.generate(task, False, self.root / f'{name}-{multi}')
                self.assert_success(app)
                self.assertEqual('completed', app._last_task_record['status'], result.issues)
                self.assertFalse(result.issues)
                self.assertEqual(before, (out / 'oldVersion/Demo/a.txt').read_bytes())
                self.assertEqual(after, (out / 'newVersion/Demo/a.txt').read_bytes())
                self.assert_no_stages(out)

    def test_commit_tree_and_blob_replacements_match_native_exports_in_both_modes(self):
        for kind in ('commit', 'tree', 'blob'):
            with self.subTest(kind=kind):
                repo, old, new, _replacement = self.repository(kind, kind)
                self.assertEqual(b'replaced\r\n', self.native(repo, new))
                self.check_workers(repo, old, new, kind)

    def test_custom_replace_namespace_and_disabled_replacement_policies_match_native(self):
        cases = (('custom', {'GIT_REPLACE_REF_BASE': 'refs/custom-replace/'}, None),
                 ('environment', {'GIT_NO_REPLACE_OBJECTS': '1'}, None),
                 ('config', {}, 'false'))
        for name, environment, policy in cases:
            with self.subTest(case=name), mock.patch.dict(os.environ, environment):
                prefix = environment.get('GIT_REPLACE_REF_BASE', 'refs/replace/')
                repo, old, new, _replacement = self.repository(name, prefix=prefix)
                if policy is not None:
                    repo.cmd('config', 'core.useReplaceRefs', policy)
                self.check_workers(repo, old, new, name)

    def test_completed_snapshot_keeps_body_attributes_and_one_shot_exports_fixed(self):
        repo, old, new, _replacement = self.repository('fixed')
        expected = self.native(repo, new)
        vcs = GitVCS(str(repo.path))
        self.addCleanup(vcs.cleanup)
        # 先打开旧 raw 通道；固定端点后不能继续借用源仓库的可变引用。
        self.assertEqual(b'replaced\n', vcs.get_file_content_raw_bytes(new, 'a.txt'))
        vcs.get_changed_files(old, new)
        signature = vcs.get_file_signature(new, 'a.txt')
        repo.cmd('update-ref', 'refs/replace/' + new, old)
        self.assertNotEqual(expected, self.native(repo, new))
        for timeout in (None, 10):
            with self.subTest(timeout=timeout):
                vcs.COMMAND_TIMEOUT = timeout
                self.assertEqual(expected, vcs.get_file_content_bytes(new, 'a.txt'))
                self.assertEqual('replaced\n', vcs.get_file_content(new, 'a.txt'))
                self.assertEqual(len(b'replaced\n'), vcs.get_file_size(new, 'a.txt'))
                self.assertEqual(signature, vcs.get_file_signature(new, 'a.txt'))
                output = self.root / ('fixed-' + str(timeout) + '.txt')
                vcs.export_file_to_path(new, 'a.txt', str(output))
                self.assertEqual(expected, output.read_bytes())

    def test_new_replacement_after_plain_snapshot_cannot_change_delivered_bytes(self):
        repo, old, new, replacement = self.repository('new-reference', prefix='refs/inactive/')
        vcs = GitVCS(str(repo.path))
        self.addCleanup(vcs.cleanup)
        vcs.get_changed_files(old, new)
        expected = self.native(repo, new)
        repo.cmd('update-ref', 'refs/replace/' + new, replacement)
        self.assertNotEqual(expected, self.native(repo, new))
        output = self.root / 'unchanged-plain.txt'
        vcs.export_file_to_path(new, 'a.txt', str(output))
        self.assertEqual(expected, output.read_bytes())

    def test_replace_change_during_diff_is_rejected_before_snapshot_publication(self):
        repo, old, new, _replacement = self.repository('changing')
        vcs = GitVCS(str(repo.path))
        self.addCleanup(vcs.cleanup)
        original = vcs._run_bytes

        def mutate(args):
            result = original(args)
            if args[0] == 'diff':
                repo.cmd('update-ref', 'refs/replace/' + new, old)
            return result

        with mock.patch.object(vcs, '_run_bytes', side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, '替换引用'):
                vcs.get_changed_files(old, new)
        self.assertIsNone(vcs._checkout_snapshot)

    def test_fixed_snapshot_metadata_keeps_explicit_command_output_policy(self):
        repo, _old, new, _replacement = self.repository('command-policy')
        vcs = GitMultiVersionVCS(str(repo.path), [new])
        self.addCleanup(vcs.cleanup)
        with mock.patch.object(GitMultiVersionVCS, 'MAX_GIT_COMMAND_OUTPUT_BYTES', 1):
            with self.assertRaisesRegex(RuntimeError, '输出'):
                vcs._git_bytes('ls-tree', '-r', '--name-only', new)

    def test_replacement_cannot_enable_execution_of_external_filters(self):
        repo, old, new, _replacement = self.repository('filter')
        candidate = repo.commit({'.gitattributes': b'*.txt filter=Forbidden\n'})
        tree = repo.cmd('rev-parse', candidate + '^{tree}')
        replacement = repo.cmd('commit-tree', tree, '-p', old, '-m', 'filter replacement')
        repo.cmd('update-ref', 'HEAD', new)
        repo.cmd('update-ref', 'refs/replace/' + new, replacement)
        marker = self.root / 'filter-executed'
        repo.cmd('config', 'filter.Forbidden.smudge', f'echo executed > "{marker.as_posix()}"')
        for multi in (False, True):
            with self.subTest(multi=multi):
                app, result, out = self.generate(repo.task(new if multi else old, new, multi=multi),
                                                  False, self.root / f'filter-{multi}')
                self.assert_success(app)
                self.assertEqual('partial', app._last_task_record['status'])
                self.assertTrue(result.delivery_incomplete)
                self.assertFalse((out / 'newVersion/Demo/a.txt').exists())
                self.assertEqual(b'new good\n', (out / 'newVersion/Demo/good.py').read_bytes())
                self.assertFalse(marker.exists())
                self.assert_no_stages(out)


class ArchiveRenameModeTests(WorkflowCase):
    def task(self, old, new, **options):
        return dict(vcs_type='archive', project_path='', project_name='Demo',
                    old_version=str(old), new_version=str(new), exclude_rules='', **options)

    def test_zip_and_tar_rename_report_only_real_permission_changes(self):
        cases = ((0o600, 0o644, None), (0o755, 0o644, 'chmod -x'),
                 (0o644, 0o755, 'chmod +x'), (0o644, 0o644, None))
        for suffix in ('.zip', '.tar'):
            for before, after, permission in cases:
                with self.subTest(suffix=suffix, before=before, after=after):
                    name = f'{suffix[1:]}-{before}-{after}'
                    old, new = self.root / ('old-' + name + suffix), self.root / ('new-' + name + suffix)
                    write_archive(old, {'before.conf': b'unchanged\n'}, mode=before)
                    write_archive(new, {'after.conf': b'unchanged\n'}, mode=after)
                    app, result, out = self.generate(self.task(old, new), False, self.root / name)
                    self.assert_success(app)
                    self.assertFalse(result.issues)
                    self.assertEqual([('R', 'before.conf', 'after.conf')],
                                     [(f.report_type, f.old_path, f.file_path) for f in result.files])
                    note = f'压缩包文件模式：{before:04o} → {after:04o}'
                    expected = [note] if before != after else []
                    self.assertEqual(expected, result.files[0].metadata_changes)
                    report = (out / 'report.html').read_text('utf-8')
                    if expected:
                        self.assertIn(note, report)
                    else:
                        self.assertNotIn('压缩包文件模式：', report)
                    instructions = next(out.glob('*操作说明.txt')).read_text('utf-8-sig')
                    if permission:
                        self.assertIn(permission, instructions)
                    self.assertEqual(b'unchanged\n', (out / 'oldVersion/Demo/before.conf').read_bytes())
                    self.assertEqual(b'unchanged\n', (out / 'newVersion/Demo/after.conf').read_bytes())
                    self.assert_no_stages(out)

    def test_rebased_roots_and_one_sided_exclusions_keep_correct_metadata(self):
        for suffix in ('.zip', '.tar'):
            old, new = self.root / ('root-old' + suffix), self.root / ('root-new' + suffix)
            write_archive(old, {'v1/before.conf': b'unchanged'}, mode=0o600)
            write_archive(new, {'v2/after.conf': b'unchanged'}, mode=0o644)
            for index, (exclude, expected_type) in enumerate((('', 'R'), ('before.conf', 'A'), ('after.conf', 'D'))):
                task = self.task(old, new, ignore_archive_root=True)
                task['exclude_rules'] = exclude
                app, result, out = self.generate(task, False, self.root / f'roots-{suffix[1:]}-{index}')
                self.assert_success(app)
                self.assertFalse(result.issues)
                self.assertEqual([expected_type], [f.report_type for f in result.files])
                expected = ['压缩包文件模式：0600 → 0644'] if expected_type == 'R' else []
                self.assertEqual(expected, result.files[0].metadata_changes)
                self.assert_no_stages(out)

    def test_recursive_member_permission_notes_preserve_original_package_bytes(self):
        for suffix in ('.zip', '.tar'):
            before, after = self.root / ('folder-old-' + suffix[1:]), self.root / ('folder-new-' + suffix[1:])
            before.mkdir()
            after.mkdir()
            old, new = before / ('app' + suffix), after / ('app' + suffix)
            write_archive(old, {'before.conf': b'unchanged'}, mode=0o600)
            write_archive(new, {'after.conf': b'unchanged'}, mode=0o644)
            old_bytes, new_bytes = old.read_bytes(), new.read_bytes()
            task = dict(vcs_type='folder', project_path='', project_name='Demo',
                        old_version=str(before), new_version=str(after), exclude_rules='')
            app, result, out = self.generate(task, True, self.root / ('recursive-' + suffix[1:]))
            self.assert_success(app)
            member = result.files[0].archive_details['members'][0]
            self.assertEqual('R', member.report_type)
            self.assertEqual(['压缩包文件模式：0600 → 0644'], member.metadata_changes)
            self.assertIn('压缩包文件模式：0600 → 0644', (out / 'report.html').read_text('utf-8'))
            self.assertEqual(old_bytes, (out / 'oldVersion/Demo' / old.name).read_bytes())
            self.assertEqual(new_bytes, (out / 'newVersion/Demo' / new.name).read_bytes())
            self.assertEqual(old_bytes, old.read_bytes())
            self.assertEqual(new_bytes, new.read_bytes())
            self.assert_no_stages(out)
