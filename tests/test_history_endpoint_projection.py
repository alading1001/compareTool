"""Real repositories and worker outputs for final-endpoint exclusion semantics."""
from pathlib import Path
from unittest import mock

from archive_workflow_fixtures import Repository, WorkflowCase
from vcs.multi_version_vcs import GitMultiVersionVCS, SVNMultiVersionVCS
from test_archive_report import zip_bytes
from diff_engine import DiffEngine
from vcs.folder_vcs import FolderVCS


class HistoryEndpointProjectionTests(WorkflowCase):
    def assert_delivery(self, task, expected, name):
        app, result, out = self.generate(task, output=self.root / name)
        self.assert_success(app)
        self.assertFalse(result.delivery_incomplete, result.issues)
        actual = {p.relative_to(out).as_posix(): p.read_bytes()
                  for side in ('oldVersion', 'newVersion')
                  for p in (out / side).rglob('*') if p.is_file()}
        self.assertEqual(expected, actual)
        report = (out / 'report.html').read_text(encoding='utf-8')
        instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8')
        for file in result.files:
            self.assertIn(file.file_path, report)
            if file.change_type.value in ('D', 'R'):
                self.assertIn('Demo/' + file.file_path, instructions)
            if file.old_path:
                self.assertIn('Demo/' + file.old_path, instructions)
        self.assertIn(f'删除文件：{sum(f.change_type.value == "D" for f in result.files)} 个', instructions)
        self.assertIn(f'重命名文件：{sum(f.change_type.value == "R" for f in result.files)} 个', instructions)
        self.assert_no_stages(out)
        return result, instructions

    def test_partial_svn_fork_is_replanned_before_final_projection(self):
        repo = Repository(self, 'svn', 'fork')
        repo.commit({'old/a.txt': b'A0\n', 'old/sub/b.txt': b'unchanged\n'})
        first = repo.commit({'old/a.txt': b'A1\n'})
        repo.cmd('copy', 'old', 'logs')
        repo.cmd('move', 'old', 'release')
        repo.commit()
        last = repo.commit({'release/a.txt': b'A2\n'})
        base = {'oldVersion/Demo/old/a.txt': b'A0\n',
                'newVersion/Demo/release/a.txt': b'A2\n'}
        for index, patterns in enumerate(('', 'logs/a.txt', 'logs/*', 'logs/**',
                                           'old/**\nlogs/**', 'release/**\nlogs/**')):
            with self.subTest(patterns=patterns):
                expected = dict(base)
                if not patterns:
                    expected['newVersion/Demo/logs/a.txt'] = b'A1\n'
                if 'old/**' in patterns:
                    del expected['oldVersion/Demo/old/a.txt']
                if 'release/**' in patterns:
                    del expected['newVersion/Demo/release/a.txt']
                task = repo.task(first + ', ' + last, '', multi=True)
                task['exclude_rules'] = patterns
                result, instructions = self.assert_delivery(task, expected, 'fork-out-' + str(index))
                if 'old/**' not in patterns:
                    self.assertIn(('D', 'old/a.txt'),
                                  [(f.change_type.value, f.file_path) for f in result.files])
                self.assertNotIn('sub/b.txt', instructions)

    def test_intermediate_exclusion_does_not_break_identity_or_change_bytes(self):
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind + '-chain')
            repo.commit({'app/a.txt': b'old\n', 'logs/anchor': b'fixed'})
            first = repo.commit({'app/a.txt': b'selected-one\n'})
            repo.cmd('mv', 'app/a.txt', 'logs/a.txt')
            repo.commit()
            repo.cmd('mv', 'logs/a.txt', 'app/b.txt')
            repo.commit()
            last = repo.commit({'app/b.txt': b'selected-two\n'})
            for index, patterns in enumerate(('', 'logs/**', 'app/a.txt', 'app/b.txt',
                                               'app/a.txt\napp/b.txt')):
                expected = {}
                if 'app/a.txt' not in patterns:
                    expected['oldVersion/Demo/app/a.txt'] = b'old\n'
                if 'app/b.txt' not in patterns:
                    expected['newVersion/Demo/app/b.txt'] = b'selected-two\n'
                task = repo.task(first + ', ' + last, '', multi=True)
                task['exclude_rules'] = patterns
                self.assert_delivery(task, expected, kind + '-chain-out-' + str(index))

    def test_multiversion_snapshot_rules_are_immutable(self):
        for kind, cls in (('git', GitMultiVersionVCS), ('svn', SVNMultiVersionVCS)):
            repo = Repository(self, kind, kind + '-rules')
            repo.commit({'keep': b'old', 'logs/skip': b'old'})
            last = repo.commit({'keep': b'new', 'logs/skip': b'new'})
            vcs = cls(str(repo.path), [last], exclude_patterns=['logs/**'])
            try:
                vcs.set_exclude_patterns([' logs/** '])
                before = vcs.get_changed_files()
                with self.assertRaisesRegex(ValueError, '重新创建'):
                    vcs.set_exclude_patterns([])
                self.assertEqual(before, vcs.get_changed_files())
                vcs.exclude_patterns = []
                with self.assertRaisesRegex(ValueError, '重新创建'):
                    vcs.get_changed_files()
            finally:
                vcs.cleanup()

    def test_excluded_endpoints_are_not_materialized(self):
        repo = Repository(self, 'svn', 'not-read')
        repo.commit({'keep': b'old', 'logs/skip': b'old'})
        last = repo.commit({'keep': b'new', 'logs/skip': b'new'})
        original = SVNMultiVersionVCS._write_svn_raw_endpoint
        def checked(vcs, version, path, destination):
            self.assertFalse(path.startswith('logs/'))
            return original(vcs, version, path, destination)
        with mock.patch.object(SVNMultiVersionVCS, '_write_svn_raw_endpoint', checked):
            vcs = SVNMultiVersionVCS(str(repo.path), [last], exclude_patterns=['logs/**'])
            try:
                self.assertEqual(['keep'], [f.path for f in vcs.get_changed_files()])
            finally:
                vcs.cleanup()

    def test_chained_unselected_svn_forks_do_not_leave_intermediate_aliases(self):
        repo = Repository(self, 'svn', 'chained-forks')
        repo.commit({'old/a': b'zero'})
        first = repo.commit({'old/a': b'one'})
        repo.cmd('copy', 'old', 'logs'); repo.cmd('move', 'old', 'middle')
        repo.commit()
        repo.cmd('copy', 'middle', 'stash'); repo.cmd('move', 'middle', 'release')
        repo.commit()
        last = repo.commit({'release/a': b'two'})
        task = repo.task(first + ', ' + last, '', multi=True)
        task['exclude_rules'] = 'logs/**\nstash/**'
        self.assert_delivery(task, {'oldVersion/Demo/old/a': b'zero',
                                   'newVersion/Demo/release/a': b'two'}, 'chained-output')

    def test_svn_fork_overwriting_existing_directory_keeps_both_real_sides(self):
        repo = Repository(self, 'svn', 'overwrite-fork')
        repo.commit({'old/a': b'zero', 'target/a': b'previous target'})
        first = repo.commit({'old/a': b'one'})
        repo.cmd('delete', 'target'); repo.cmd('copy', 'old', 'target')
        repo.cmd('move', 'old', 'release')
        repo.commit()
        last = repo.commit({'release/a': b'two'})
        task = repo.task(first + ', ' + last, '', multi=True)
        self.assert_delivery(task, {'oldVersion/Demo/old/a': b'zero',
                                   'oldVersion/Demo/target/a': b'previous target',
                                   'newVersion/Demo/target/a': b'one',
                                   'newVersion/Demo/release/a': b'two'}, 'overwrite-output')

    def test_svn_ordinary_copy_does_not_expand_selected_source_endpoints(self):
        repo = Repository(self, 'svn', 'ordinary-copy')
        repo.commit({'source/a': b'zero'})
        first = repo.commit({'source/a': b'one'})
        repo.cmd('copy', 'source', 'copy'); repo.commit()
        last = repo.commit({'copy/a': b'two'})
        task = repo.task(first + ', ' + last, '', multi=True)
        self.assert_delivery(task, {'oldVersion/Demo/source/a': b'zero',
                                   'newVersion/Demo/source/a': b'one',
                                   'oldVersion/Demo/copy/a': b'one',
                                   'newVersion/Demo/copy/a': b'two'}, 'ordinary-output')

    def test_svn_fork_path_reused_as_directory_does_not_hide_real_files(self):
        repo = Repository(self, 'svn', 'fork-directory')
        repo.commit({'old/a': b'zero'})
        first = repo.commit({'old/a': b'one'})
        repo.cmd('copy', 'old', 'logs'); repo.cmd('move', 'old', 'release')
        repo.commit()
        last = repo.commit({'release/a': b'two', 'old/a/child': b'new child'})
        task = repo.task(first + ', ' + last, '', multi=True)
        task['exclude_rules'] = 'logs/**'
        self.assert_delivery(task, {'oldVersion/Demo/old/a': b'zero',
                                   'newVersion/Demo/release/a': b'two',
                                   'newVersion/Demo/old/a/child': b'new child'}, 'fork-directory-output')

    def test_svn_fork_replacing_old_directory_keeps_descendant_deletions(self):
        repo = Repository(self, 'svn', 'fork-file')
        repo.commit({'old/a': b'zero', 'target/a/child': b'old child'})
        first = repo.commit({'old/a': b'one'})
        repo.cmd('delete', 'target'); repo.cmd('copy', 'old', 'target')
        repo.cmd('move', 'old', 'release'); repo.commit()
        last = repo.commit({'release/a': b'two'})
        task = repo.task(first + ', ' + last, '', multi=True)
        result, instructions = self.assert_delivery(task, {
            'oldVersion/Demo/old/a': b'zero', 'oldVersion/Demo/target/a/child': b'old child',
            'newVersion/Demo/release/a': b'two', 'newVersion/Demo/target/a': b'one'}, 'fork-file-output')
        self.assertEqual(['target/a'], result.required_directory_deletions)
        self.assertIn('[删除旧目录后再写入同名文件] Demo/target/a', instructions)

    def test_directory_replacement_cannot_delete_excluded_old_children(self):
        old = {'foo/skip': b'excluded', 'foo/keep': b'old child', 'keep': b'old'}
        new = {'foo': b'new file', 'keep': b'new'}
        tasks = []
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind + '-replacement')
            first = repo.commit(dict(old, **{'discard/anchor': b'anchor'}))
            repo.cmd('mv', 'foo', 'discard/old')
            last = repo.commit(new)
            tasks.extend((repo.task(first, last), repo.task(last, '', multi=True)))
        folders = []
        archives = []
        for side, files in (('old', old), ('new', new)):
            root = self.root / side; root.mkdir()
            for path, data in files.items():
                destination = root / path; destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            folders.append(str(root))
            archive = self.root / (side + '.zip'); archive.write_bytes(zip_bytes(files))
            archives.append(str(archive))
        for kind, paths in (('folder', folders), ('archive', archives)):
            tasks.append(dict(vcs_type=kind, old_version=paths[0], new_version=paths[1], project_name='Demo'))
        for index, task in enumerate(tasks):
            with self.subTest(mode=task['vcs_type']):
                task['exclude_rules'] = 'foo/skip\ndiscard/**'
                app, result, out = self.generate(task, output=self.root / ('replace-' + str(index)))
                self.assert_success(app)
                self.assertTrue(result.delivery_incomplete)
                self.assertEqual(['keep'], [f.file_path for f in result.files])
                self.assertEqual([], result.required_directory_deletions)
                actual = {p.relative_to(out).as_posix(): p.read_bytes()
                          for side in ('oldVersion', 'newVersion')
                          for p in (out / side).rglob('*') if p.is_file()}
                self.assertEqual({'oldVersion/Demo/keep': b'old', 'newVersion/Demo/keep': b'new'}, actual)
                self.assertIn('间接删除', next(out.glob('*操作说明.txt')).read_text(encoding='utf-8'))
        strict = FolderVCS(*folders)
        strict.set_exclude_patterns(['foo/skip'])
        try:
            with self.assertRaisesRegex(RuntimeError, '间接删除'):
                DiffEngine(strict).generate_diff('old', 'new')
        finally:
            strict.cleanup()

    def test_new_directory_cannot_implicitly_replace_excluded_old_file(self):
        old = {'foo': b'protected old file', 'keep': b'old'}
        new = {'foo/a': b'new child', 'foo/sub/b': b'nested child', 'keep': b'new'}
        tasks = []
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind + '-reverse')
            first = repo.commit(dict(old, **{'discard/anchor': b'anchor'}))
            repo.cmd('mv', 'foo', 'discard/old')
            last = repo.commit(new)
            tasks.extend((repo.task(first, last), repo.task(last, '', multi=True)))
        paths = []
        archives = []
        for side, contents in (('old', old), ('new', new)):
            root = self.root / side; root.mkdir()
            for name, data in contents.items():
                target = root / name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            paths.append(str(root))
            archive = self.root / (side + '.zip'); archive.write_bytes(zip_bytes(contents))
            archives.append(str(archive))
        for kind, pair in (('folder', paths), ('archive', archives)):
            tasks.append(dict(vcs_type=kind, project_name='Demo', old_version=pair[0], new_version=pair[1]))
        for index, task in enumerate(tasks):
            with self.subTest(mode=task['vcs_type']):
                task['exclude_rules'] = 'foo\ndiscard/**'
                app, result, out = self.generate(task, output=self.root / ('reverse-' + str(index)))
                self.assert_success(app)
                self.assertTrue(result.delivery_incomplete)
                self.assertEqual(['keep'], [f.file_path for f in result.files])
                actual = {p.relative_to(out).as_posix(): p.read_bytes()
                          for side in ('oldVersion', 'newVersion')
                          for p in (out / side).rglob('*') if p.is_file()}
                self.assertEqual({'oldVersion/Demo/keep': b'old', 'newVersion/Demo/keep': b'new'}, actual)
                self.assertEqual([], result.required_directory_deletions)
                self.assertNotIn('[删除文件] Demo/foo', next(out.glob('*操作说明.txt')).read_text(encoding='utf-8'))
