"""SVN directory move structure survives filtering of either file endpoint."""
from archive_workflow_fixtures import Repository, WorkflowCase
from vcs.multi_version_vcs import SVNMultiVersionVCS


class SVNExclusionMoveBoundaryTests(WorkflowCase):
    def files_for(self, repo, revision, patterns):
        vcs = SVNMultiVersionVCS(str(repo.path), [revision], exclude_patterns=patterns)
        try:
            self.assertEqual([], vcs.issues)
            files = vcs.get_changed_files()
            result = {(item.change_type.value, item.path, item.old_path) for item in files}
            for item in files:
                if item.change_type.value != 'A':
                    old_path = item.old_path or item.path
                    self.assertIsNotNone(vcs.get_file_content_bytes('old', old_path))
                if item.change_type.value != 'D':
                    self.assertIsNotNone(vcs.get_file_content_bytes('new', item.path))
            return result
        finally:
            vcs.cleanup()

    def test_directory_move_child_rename_filters_each_real_endpoint(self):
        repo = Repository(self, 'svn', 'directory')
        repo.commit({'old/a.txt': b'A\n', 'old/c.txt': b'C\n',
                     'old/sub/k.txt': b'K\n'})
        repo.cmd('move', 'old', 'new')
        repo.cmd('move', 'new/a.txt', 'new/b.txt')
        revision = repo.commit()
        other_renames = {('R', 'new/c.txt', 'old/c.txt'),
                         ('R', 'new/sub/k.txt', 'old/sub/k.txt')}
        for patterns, expected in (
                ([], other_renames | {('R', 'new/b.txt', 'old/a.txt')}),
                (['old/a.txt'], other_renames | {('A', 'new/b.txt', '')}),
                (['old/**'], {('A', name, '') for name in
                              ('new/b.txt', 'new/c.txt', 'new/sub/k.txt')}),
                (['new/b.txt'], other_renames | {('D', 'old/a.txt', '')}),
                (['new/**'], {('D', name, '') for name in
                              ('old/a.txt', 'old/c.txt', 'old/sub/k.txt')}),
                (['old/*'], {('A', 'new/b.txt', ''), ('A', 'new/c.txt', ''),
                             ('R', 'new/sub/k.txt', 'old/sub/k.txt')}),
                (['new/*'], {('D', 'old/a.txt', ''), ('D', 'old/c.txt', ''),
                             ('R', 'new/sub/k.txt', 'old/sub/k.txt')})):
            with self.subTest(patterns=patterns):
                self.assertEqual(expected, self.files_for(repo, revision, patterns))

    def test_child_moved_out_of_directory_ignores_excluded_source(self):
        repo = Repository(self, 'svn', 'external')
        repo.commit({'old/a.txt': b'A\n', 'old/c.txt': b'C\n',
                     'outside/anchor': b'fixed\n'})
        repo.cmd('move', 'old', 'new')
        repo.cmd('move', 'new/a.txt', 'outside/b.txt')
        revision = repo.commit()
        retained = {('R', 'new/c.txt', 'old/c.txt')}
        for patterns, expected in (
                (['old/a.txt'], retained | {('A', 'outside/b.txt', '')}),
                (['old/**'], {('A', 'new/c.txt', ''), ('A', 'outside/b.txt', '')}),
                (['outside/**'], retained | {('D', 'old/a.txt', '')})):
            with self.subTest(patterns=patterns):
                self.assertEqual(expected, self.files_for(repo, revision, patterns))

    def test_nested_directory_move_keeps_only_unexcluded_endpoints(self):
        repo = Repository(self, 'svn', 'nested')
        repo.commit({'old/sub/a.txt': b'A\n', 'old/c.txt': b'C\n'})
        repo.cmd('move', 'old', 'new')
        repo.cmd('move', 'new/sub', 'final')
        repo.cmd('move', 'final/a.txt', 'final/b.txt')
        revision = repo.commit()
        retained = {('R', 'new/c.txt', 'old/c.txt')}
        for patterns, expected in (
                (['old/sub/a.txt'], retained | {('A', 'final/b.txt', '')}),
                (['old/sub/**'], retained | {('A', 'final/b.txt', '')}),
                (['final/**'], retained | {('D', 'old/sub/a.txt', '')})):
            with self.subTest(patterns=patterns):
                self.assertEqual(expected, self.files_for(repo, revision, patterns))

    def test_ordinary_child_copy_keeps_inherited_identity(self):
        repo = Repository(self, 'svn', 'ordinary-copy')
        repo.commit({'old/a.txt': b'A\n'})
        repo.cmd('move', 'old', 'new')
        repo.cmd('copy', 'new/a.txt', 'new/b.txt')
        revision = repo.commit()
        for patterns, expected in (
                ([], {('R', 'new/a.txt', 'old/a.txt'), ('A', 'new/b.txt', '')}),
                (['old/a.txt'], {('A', 'new/a.txt', ''), ('A', 'new/b.txt', '')}),
                (['old/**'], {('A', 'new/a.txt', ''), ('A', 'new/b.txt', '')}),
                (['new/b.txt'], {('R', 'new/a.txt', 'old/a.txt')})):
            with self.subTest(patterns=patterns):
                self.assertEqual(expected, self.files_for(repo, revision, patterns))

    def test_excluded_fork_target_does_not_change_child_identity(self):
        repo = Repository(self, 'svn', 'fork')
        repo.commit({'old/a.txt': b'A\n'})
        repo.cmd('move', 'old', 'new')
        repo.cmd('copy', 'new/a.txt', 'new/b.txt')
        repo.cmd('move', 'new/a.txt', 'new/c.txt')
        revision = repo.commit()
        for patterns, expected in (
                ([], {('D', 'old/a.txt', ''), ('A', 'new/b.txt', ''),
                      ('A', 'new/c.txt', '')}),
                (['old/a.txt'], {('A', 'new/b.txt', ''), ('A', 'new/c.txt', '')}),
                (['new/b.txt'], {('D', 'old/a.txt', ''), ('A', 'new/c.txt', '')}),
                (['new/c.txt'], {('D', 'old/a.txt', ''), ('A', 'new/b.txt', '')})):
            with self.subTest(patterns=patterns):
                self.assertEqual(expected, self.files_for(repo, revision, patterns))

    def test_excluded_directory_copy_does_not_change_fork_identity(self):
        repo = Repository(self, 'svn', 'directory-fork')
        repo.commit({'old/a.txt': b'A\n', 'old/sub/k.txt': b'K\n',
                     'logs/anchor': b'fixed\n'})
        repo.cmd('copy', 'old', 'logs/copy')
        repo.cmd('move', 'old', 'release')
        revision = repo.commit()
        sources = ('a.txt', 'sub/k.txt')
        for patterns, expected in (
                ([], {('D', 'old/' + suffix, '') for suffix in sources}
                 | {('A', prefix + suffix, '') for prefix in ('release/', 'logs/copy/')
                    for suffix in sources}),
                (['logs/**'], {('D', 'old/' + suffix, '') for suffix in sources}
                 | {('A', 'release/' + suffix, '') for suffix in sources}),
                (['old/**', 'logs/**'], {('A', 'release/' + suffix, '')
                                         for suffix in sources}),
                (['release/**'], {('D', 'old/' + suffix, '') for suffix in sources}
                 | {('A', 'logs/copy/' + suffix, '') for suffix in sources})):
            with self.subTest(patterns=patterns):
                self.assertEqual(expected, self.files_for(repo, revision, patterns))
