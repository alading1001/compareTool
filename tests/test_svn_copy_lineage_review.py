"""SVN copy/move combinations must deliver the real selected file endpoints."""
from archive_workflow_fixtures import Repository, WorkflowCase


class SVNCopyLineageReviewTests(WorkflowCase):
    def assert_delivery(self, repo, versions, patterns, expected, name, changes):
        task = repo.task(','.join(versions), '', multi=True)
        task['exclude_rules'] = patterns
        app, result, output = self.generate(task, enabled=False, output=self.root / name)
        self.assert_success(app)
        self.assertFalse(result.delivery_incomplete, result.issues)
        actual = {p.relative_to(output).as_posix(): p.read_bytes()
                  for side in ('oldVersion', 'newVersion')
                  for p in (output / side).rglob('*') if p.is_file()}
        self.assertEqual(expected, actual)
        self.assertEqual(changes, {(f.change_type.value, f.file_path, f.old_path)
                                   for f in result.files})
        self.assertEqual(changes, {(f.change_type.value, f.file_path, f.old_path)
                                   for f in result.report_manifest_files})
        instructions = next(output.glob('*操作说明.txt')).read_text(encoding='utf-8')
        report = (output / 'report.html').read_text(encoding='utf-8')
        for kind, path, old_path in changes:
            self.assertIn(path, report)
            if kind == 'D':
                self.assertIn('[删除文件] Demo/' + path, instructions)
            elif kind == 'R':
                self.assertIn('[重命名旧路径] Demo/' + old_path, instructions)
        self.assertIn(f'删除文件：{sum(c[0] == "D" for c in changes)} 个', instructions)
        self.assertIn(f'重命名文件：{sum(c[0] == "R" for c in changes)} 个', instructions)
        self.assertEqual(len(changes), app._last_task_record['counts']['total_files'])
        self.assert_no_stages(output)
        return result

    def fork(self, name, operation):
        repo = Repository(self, 'svn', name)
        repo.commit({'old/a': b'zero'})
        first = repo.commit({'old/a': b'one'})
        repo.cmd('copy', 'old', 'logs')
        repo.cmd('move', 'old', 'release')
        if operation == 'delete':
            repo.cmd('delete', 'logs/a')
            target = 'release/a'
        else:
            repo.cmd('move', 'release/a', 'release/b')
            target = 'release/b'
        middle = repo.commit()
        last = repo.commit({target: b'two'})
        return repo, first, middle, last, target

    def test_only_surviving_copy_keeps_identity_across_unselected_revision(self):
        repo, first, middle, last, target = self.fork('survivor', 'delete')
        for index, patterns in enumerate(('', 'logs/**')):
            self.assert_delivery(repo, [first, last], patterns,
                {'oldVersion/Demo/old/a': b'zero', 'newVersion/Demo/' + target: b'two'},
                'survivor-' + str(index), {('R', target, 'old/a')})

    def test_selected_copy_then_delete_has_no_nonexistent_old_endpoint(self):
        repo, first, middle, last, target = self.fork('selected-survivor', 'delete')
        self.assert_delivery(repo, [middle], '',
            {'oldVersion/Demo/old/a': b'one', 'newVersion/Demo/' + target: b'one'},
            'selected-survivor-output', {('R', target, 'old/a')})

    def test_fork_and_child_rename_keeps_every_real_copy_before_exclusions(self):
        repo, first, middle, last, target = self.fork('renamed-fork', 'rename')
        for index, patterns in enumerate(('', 'logs/**')):
            expected = {'oldVersion/Demo/old/a': b'zero',
                        'newVersion/Demo/' + target: b'two'}
            changes = {('D', 'old/a', ''), ('A', target, '')}
            if not patterns:
                expected['newVersion/Demo/logs/a'] = b'one'
                changes.add(('A', 'logs/a', ''))
            self.assert_delivery(repo, [first, last], patterns, expected,
                                 'renamed-fork-' + str(index), changes)

    def test_excluded_inherited_replacement_does_not_block_independent_file(self):
        repo = Repository(self, 'svn', 'excluded-replacement')
        repo.commit({'source/a': b'source', 'keep': b'zero'})
        first = repo.commit({'keep': b'one'})
        repo.cmd('copy', 'source', 'logs')
        repo.cmd('delete', 'logs/a')
        repo.write({'logs/a': b'recreated'})
        repo.cmd('add', 'logs/a')
        middle = repo.commit()
        last = repo.commit({'keep': b'two'})
        for index, patterns in enumerate(('', 'logs/**')):
            self.assert_delivery(repo, [first, last], patterns,
                {'oldVersion/Demo/keep': b'zero', 'newVersion/Demo/keep': b'two'},
                'independent-' + str(index), {('M', 'keep', '')})
        self.assert_delivery(repo, [middle], '', {'newVersion/Demo/logs/a': b'recreated'},
                             'selected-replacement', {('A', 'logs/a', '')})
