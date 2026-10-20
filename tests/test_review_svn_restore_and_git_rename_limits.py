"""真实 SVN 根恢复及 Git 配置限制造成的交付语义回归。"""
from archive_workflow_fixtures import Repository, WorkflowCase
from test_review_five_integrity_fixes import delivered
from vcs.svn_vcs import SVNVCS


class SVNOrdinaryRestoreTests(WorkflowCase):
    def check_delivery(self, repo, scope, old, new, name, before, after):
        app, result, out = self.generate(
            repo.task(old, new, scope=repo.path / scope), False, self.root / name)
        self.assert_success(app)
        self.assertEqual('completed', app._last_task_record['status'], result.issues)
        self.assertFalse(result.issues)
        self.assertEqual(before, delivered(out, 'oldVersion'))
        self.assertEqual(after, delivered(out, 'newVersion'))
        self.assert_no_stages(out)
        return result, out

    def test_root_and_ancestor_restore_preserve_both_directions_and_properties(self):
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                repo = Repository(self, 'svn', 'restore-' + str(ancestor))
                scope = 'project/child' if ancestor else 'project'
                first = repo.commit({scope + '/a.txt': b'original\n',
                                     scope + '/restored.txt': b'restored\n'})
                repo.cmd('delete', scope + '/restored.txt')
                repo.cmd('propset', 'svn:eol-style', 'CRLF', scope + '/a.txt')
                repo.cmd('propset', 'svn:executable', 'yes', scope + '/a.txt')
                before = repo.commit({scope + '/a.txt': b'updated\n',
                                      scope + '/extra.txt': b'extra\n'})
                repo.cmd('delete', 'project')
                repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / 'project'))
                restored = repo.commit()
                old_files = {'Demo/a.txt': b'updated\r\n', 'Demo/extra.txt': b'extra\n'}
                new_files = {'Demo/a.txt': b'original\n', 'Demo/restored.txt': b'restored\n'}
                for reverse in (False, True):
                    old, new = (restored, before) if reverse else (before, restored)
                    expected_old, expected_new = (new_files, old_files) if reverse else (old_files, new_files)
                    result, out = self.check_delivery(repo, scope, old, new,
                        f'out-{ancestor}-{reverse}', expected_old, expected_new)
                    changed = next(f for f in result.files if f.file_path == 'a.txt')
                    self.assertEqual((not reverse, reverse),
                                     (changed.old_executable, changed.new_executable))
                    self.assertEqual(3, len(result.report_manifest_files))
                    report = (out / 'report.html').read_text('utf8')
                    self.assertIn('updated', report)
                    self.assertIn('original', report)
                    instructions = next(out.glob('*操作说明.txt')).read_text('utf-8-sig')
                    self.assertIn('Demo/' + ('restored.txt' if reverse else 'extra.txt'), instructions)

    def test_repeated_restores_follow_later_root_move(self):
        repo = Repository(self, 'svn', 'repeated')
        first = repo.commit({'project/a.txt': b'first\n'})
        before = repo.commit({'project/a.txt': b'second\n'})
        repo.cmd('delete', 'project')
        repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / 'project'))
        restored = repo.commit()
        changed = repo.commit({'project/a.txt': b'third\n'})
        repo.cmd('delete', 'project')
        repo.cmd('copy', repo.url + '/project@' + restored, str(repo.path / 'project'))
        final = repo.commit()
        repo.cmd('move', 'project', 'destination')
        repo.commit()
        self.check_delivery(repo, 'destination', before, final, 'before-first-restore',
            {'Demo/a.txt': b'second\n'}, {'Demo/a.txt': b'first\n'})
        self.check_delivery(repo, 'destination', changed, final, 'before-second-restore',
            {'Demo/a.txt': b'third\n'}, {'Demo/a.txt': b'first\n'})

    def test_stale_move_uses_source_history_instead_of_overwritten_target(self):
        repo = Repository(self, 'svn', 'move')
        first = repo.commit({'container/project/a.txt': b'first',
                             'destination/unrelated.txt': b'unrelated'})
        before = repo.commit({'container/project/a.txt': b'second',
                              'container/project/extra.txt': b'extra'})
        repo.cmd('delete', 'destination')
        repo.cmd('copy', repo.url + '/container/project@' + first, str(repo.path / 'destination'))
        repo.cmd('delete', 'container')
        restored = repo.commit()
        self.check_delivery(repo, 'destination', before, restored, 'out',
            {'Demo/a.txt': b'second', 'Demo/extra.txt': b'extra'},
            {'Demo/a.txt': b'first'})

    def test_restore_keeps_pinned_identity_after_working_copy_switch(self):
        repo = Repository(self, 'svn', 'switch')
        first = repo.commit({'project/a.txt': b'first', 'other/a.txt': b'other'})
        before = repo.commit({'project/a.txt': b'second'})
        repo.cmd('delete', 'project')
        repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / 'project'))
        restored = repo.commit()
        vcs = SVNVCS(str(repo.path / 'project'))
        self.addCleanup(vcs.cleanup)
        self.assertTrue(vcs.check_version_exists(before))
        repo.cmd('switch', '--ignore-ancestry', repo.url + '/other', 'project')
        self.assertEqual(['a.txt'], [f.path for f in vcs.get_changed_files(before, restored)])
        self.assertEqual(b'second', vcs.get_file_content_raw_bytes(before, 'a.txt'))
        self.assertEqual(b'first', vcs.get_file_content_raw_bytes(restored, 'a.txt'))

    def test_ordinary_copy_does_not_borrow_unrelated_preexisting_target(self):
        repo = Repository(self, 'svn', 'copy')
        first = repo.commit({'source/a.txt': b'source', 'destination/a.txt': b'unrelated'})
        before = repo.commit({'destination/a.txt': b'unrelated later'})
        repo.cmd('delete', 'destination')
        repo.cmd('copy', repo.url + '/source@' + first, str(repo.path / 'destination'))
        repo.commit()
        vcs = SVNVCS(str(repo.path / 'destination'))
        self.addCleanup(vcs.cleanup)
        self.assertFalse(vcs.check_version_exists(before))
        self.assertTrue(vcs.check_version_exists(first))
        self.assertEqual(b'source', vcs.get_file_content_raw_bytes(first, 'a.txt'))


class GitRenameLimitTests(WorkflowCase):
    def test_normal_and_multi_report_renames_under_repository_and_global_limits(self):
        repo = Repository(self, 'git', 'renames')
        before = {f'old{i}.txt': (f'distinct file {i}\n' * 25).encode() for i in range(3)}
        first = repo.commit(before)
        after = {}
        for i in range(3):
            repo.cmd('mv', f'old{i}.txt', f'new{i}.txt')
            after[f'new{i}.txt'] = before[f'old{i}.txt'] + b'added\n'
        last = repo.commit(after)
        repo.cmd('config', '--global', 'diff.renameLimit', '1')
        for local in (False, True):
            if local:
                repo.cmd('config', 'diff.renameLimit', '1')
            for multi in (False, True):
                with self.subTest(local=local, multi=multi):
                    app, result, out = self.generate(repo.task(last if multi else first, last, multi=multi),
                        False, self.root / f'out-{local}-{multi}')
                    self.assert_success(app)
                    self.assertFalse(result.issues)
                    self.assertEqual('completed', app._last_task_record['status'])
                    self.assertEqual({('R', f'new{i}.txt', f'old{i}.txt') for i in range(3)},
                        {(f.report_type, f.file_path, f.old_path) for f in result.report_manifest_files})
                    self.assertEqual(3, result.summary['total_files'])
                    self.assertEqual({'Demo/' + p: data for p, data in before.items()}, delivered(out, 'oldVersion'))
                    self.assertEqual({'Demo/' + p: data for p, data in after.items()}, delivered(out, 'newVersion'))
                    self.assert_no_stages(out)

    def test_unselected_batch_rename_keeps_selected_file_identity(self):
        repo = Repository(self, 'git', 'noncontiguous')
        original = {f'old{i}.txt': (f'distinct file {i}\n' * 25).encode() for i in range(3)}
        repo.commit(original)
        selected = repo.commit({'old0.txt': original['old0.txt'] + b'selected first\n'})
        for i in range(3):
            repo.cmd('mv', f'old{i}.txt', f'new{i}.txt')
            with (repo.path / f'new{i}.txt').open('ab') as stream:
                stream.write(b'unselected rename\n')
        repo.commit()
        final_bytes = original['old0.txt'] + b'selected first\nunselected rename\nselected last\n'
        last = repo.commit({'new0.txt': final_bytes})
        repo.cmd('config', 'diff.renameLimit', '1')
        app, result, out = self.generate(repo.task(selected + ',' + last, '', multi=True),
                                         False, self.root / 'out')
        self.assert_success(app)
        self.assertFalse(result.issues)
        self.assertEqual([('R', 'new0.txt', 'old0.txt')],
                         [(f.report_type, f.file_path, f.old_path) for f in result.report_manifest_files])
        self.assertEqual({'Demo/old0.txt': original['old0.txt']}, delivered(out, 'oldVersion'))
        self.assertEqual({'Demo/new0.txt': final_bytes}, delivered(out, 'newVersion'))
        self.assert_no_stages(out)
