"""真实仓库验证子模块不能静默消失、历史复制不能制造旧文件端点。"""
import json

from archive_workflow_fixtures import Repository, WorkflowCase
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS, SVNMultiVersionVCS


class DeliveryAssertions(WorkflowCase):
    def check_delivery(self, task, name, old, new, changes, *, partial=False):
        app, result, output = self.generate(task, False, self.root / name)
        self.assert_success(app)
        self.assertEqual('partial' if partial else 'completed', app._last_task_record['status'])
        self.assertEqual(partial, result.delivery_incomplete, result.issues)
        if not partial:
            self.assertEqual([], result.issues)
        for side, expected in (('oldVersion', old), ('newVersion', new)):
            directory = output / side / 'Demo'
            actual = {p.relative_to(directory).as_posix(): p.read_bytes()
                      for p in directory.rglob('*') if p.is_file()}
            self.assertEqual(expected, actual)
        self.assertEqual(changes, {(f.report_type, f.file_path, f.old_path) for f in result.files})
        self.assertEqual(changes, {(f.report_type, f.file_path, f.old_path)
                                   for f in result.report_manifest_files})
        self.assertEqual(len(changes), result.summary['total_files'])
        self.assertEqual(len(changes), app._last_task_record['counts']['total_files'])
        report = (output / 'report.html').read_text(encoding='utf-8')
        instructions = next(output.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
        for kind, path, old_path in changes:
            self.assertIn('path: ' + json.dumps(path), report)
            if kind == 'D':
                self.assertIn('[删除文件] Demo/' + path, instructions)
            elif kind == 'R':
                self.assertIn('[重命名旧路径] Demo/' + old_path, instructions)
        self.assertIn(f'删除文件：{sum(c[0] == "D" for c in changes)} 个', instructions)
        self.assertIn(f'重命名文件：{sum(c[0] == "R" for c in changes)} 个', instructions)
        if partial:
            self.assertTrue(any(i['delivery'] and i['path'] == 'sub' for i in result.issues))
            self.assertIn('未完成', report)
            self.assertIn('交付未完成', instructions)
        self.assert_no_stages(output)
        return result


class GitSubmoduleVisibilityTests(DeliveryAssertions):
    def submodule_versions(self, name, *, independent=False, ignore='diff'):
        repo = Repository(self, 'git', name)
        attributes = '[submodule "dep"]\n\tpath = sub\n\turl = ../local-only\n'
        if ignore == 'gitmodules':
            attributes += '\tignore = all\n'
        target_old = repo.commit({'.gitmodules': attributes.encode(), 'good.txt': b'old good'})
        target_new = repo.commit({'target.txt': b'second commit object'})
        repo.cmd('update-index', '--add', '--cacheinfo', '160000,' + target_old + ',sub')
        repo.cmd('commit', '-m', 'add gitlink')
        old = repo.cmd('rev-parse', 'HEAD')
        repo.cmd('update-index', '--cacheinfo', '160000,' + target_new + ',sub')
        if independent:
            repo.write({'good.txt': b'new good'})
            repo.cmd('add', 'good.txt')
        repo.cmd('commit', '-m', 'change gitlink')
        new = repo.cmd('rev-parse', 'HEAD')
        if ignore == 'diff':
            repo.cmd('config', 'diff.ignoreSubmodules', 'all')
        elif ignore == 'submodule':
            repo.cmd('config', 'submodule.dep.ignore', 'all')
        return repo, old, new

    def test_diff_ignore_submodules_cannot_report_false_complete(self):
        for independent in (False, True):
            repo, old, new = self.submodule_versions(str(independent), independent=independent)
            expected_old = {'good.txt': b'old good'} if independent else {}
            expected_new = {'good.txt': b'new good'} if independent else {}
            changes = {('M', 'good.txt', '')} if independent else set()
            for multi in (False, True):
                with self.subTest(independent=independent, multi=multi):
                    self.check_delivery(repo.task(new if multi else old, new, multi=multi),
                        f'diff-{independent}-{multi}', expected_old, expected_new, changes, partial=True)

    def test_submodule_config_and_gitmodules_cannot_bypass_strict_or_desktop_checks(self):
        for setting in ('submodule', 'gitmodules'):
            repo, old, new = self.submodule_versions(setting, ignore=setting)
            for multi in (False, True):
                with self.subTest(setting=setting, multi=multi):
                    if multi:
                        with self.assertRaisesRegex(RuntimeError, '不是普通文件'):
                            GitMultiVersionVCS(str(repo.path), [new])
                    else:
                        vcs = GitVCS(str(repo.path))
                        try:
                            with self.assertRaisesRegex(RuntimeError, '不是普通文件'):
                                vcs.get_changed_files(old, new)
                        finally:
                            vcs.cleanup()
                    self.check_delivery(repo.task(new if multi else old, new, multi=multi),
                        f'{setting}-{multi}', {}, {}, set(), partial=True)

    def test_explicit_business_exclusion_still_allows_independent_delivery(self):
        repo, old, new = self.submodule_versions('excluded', independent=True)
        for multi in (False, True):
            task = repo.task(new if multi else old, new, multi=multi)
            task['exclude_rules'] = 'sub'
            self.check_delivery(task, 'excluded-' + str(multi),
                {'good.txt': b'old good'}, {'good.txt': b'new good'}, {('M', 'good.txt', '')})

    def test_root_commit_still_exposes_nonregular_endpoint(self):
        repo, _old, _new = self.submodule_versions('root')
        root = repo.cmd('commit-tree', repo.cmd('write-tree'), '-m', 'new root with gitlink')
        repo.cmd('update-ref', 'HEAD', root)
        task = repo.task(root, '', multi=True)
        task['exclude_rules'] = '.gitmodules\ntarget.txt\ngood.txt'
        self.check_delivery(task, 'root-output', {}, {}, set(), partial=True)


class SVNStaleDirectoryRestoreTests(DeliveryAssertions):
    def restore_repository(self, name, *, operation='keep', delayed=False):
        repo = Repository(self, 'svn', name)
        original = repo.commit({'src/a.txt': b'A', 'src/b.txt': b'B', 'good.txt': b'old good'})
        repo.cmd('delete', 'src/b.txt')
        deleted_file = repo.commit()
        if delayed:
            repo.cmd('delete', 'src')
            deleted_source = repo.commit()
        repo.cmd('copy', repo.url + '/src@' + original, str(repo.path / 'dst'))
        if operation == 'rename':
            repo.cmd('move', 'dst/b.txt', 'dst/renamed.txt')
        elif operation == 'outside':
            repo.cmd('move', 'dst/b.txt', 'outside.txt')
        elif operation == 'discard':
            repo.cmd('delete', 'dst/b.txt')
        if not delayed:
            repo.cmd('delete', 'src')
        restored = repo.commit({'good.txt': b'new good'})
        return repo, deleted_source if delayed else deleted_file, restored

    def test_restored_missing_file_and_explicit_child_moves_use_real_old_endpoint(self):
        for operation in ('keep', 'rename', 'outside', 'discard'):
            repo, deleted, restored = self.restore_repository(operation, operation=operation)
            target = {'keep': 'dst/b.txt', 'rename': 'dst/renamed.txt',
                      'outside': 'outside.txt'}.get(operation)
            for select_delete in (False, True):
                with self.subTest(operation=operation, select_delete=select_delete):
                    old = {'src/a.txt': b'A', 'good.txt': b'old good'}
                    new = {'dst/a.txt': b'A', 'good.txt': b'new good'}
                    changes = {('R', 'dst/a.txt', 'src/a.txt'), ('M', 'good.txt', '')}
                    if target:
                        new[target] = b'B'
                        changes.add(('R', target, 'src/b.txt') if select_delete else ('A', target, ''))
                    if select_delete:
                        old['src/b.txt'] = b'B'
                        if not target:
                            changes.add(('D', 'src/b.txt', ''))
                    selected = deleted + ',' + restored if select_delete else restored
                    self.check_delivery(repo.task(selected, '', multi=True),
                        f'{operation}-{select_delete}', old, new, changes)
            if operation == 'keep':
                vcs = SVNMultiVersionVCS(str(repo.path), [restored])
                try:
                    self.assertIn(('A', 'dst/b.txt'),
                        {(f.change_type.value, f.path) for f in vcs.get_changed_files()})
                finally:
                    vcs.cleanup()

    def test_restore_filtering_does_not_invent_deleted_source(self):
        repo, deleted, restored = self.restore_repository('filtered')
        for select_delete in (False, True):
            for index, patterns in enumerate(('src/**', 'dst/**', 'dst/b.txt')):
                with self.subTest(select_delete=select_delete, patterns=patterns):
                    old, new = {'good.txt': b'old good'}, {'good.txt': b'new good'}
                    changes = {('M', 'good.txt', '')}
                    if patterns == 'src/**':
                        new.update({'dst/a.txt': b'A', 'dst/b.txt': b'B'})
                        changes.update({('A', 'dst/a.txt', ''), ('A', 'dst/b.txt', '')})
                    elif patterns == 'dst/**':
                        old['src/a.txt'] = b'A'
                        changes.add(('D', 'src/a.txt', ''))
                        if select_delete:
                            old['src/b.txt'] = b'B'
                            changes.add(('D', 'src/b.txt', ''))
                    else:
                        old['src/a.txt'], new['dst/a.txt'] = b'A', b'A'
                        changes.add(('R', 'dst/a.txt', 'src/a.txt'))
                        if select_delete:
                            old['src/b.txt'] = b'B'
                            changes.add(('D', 'src/b.txt', ''))
                    selected = deleted + ',' + restored if select_delete else restored
                    task = repo.task(selected, '', multi=True)
                    task['exclude_rules'] = patterns
                    self.check_delivery(task, f'filtered-{select_delete}-{index}', old, new, changes)

    def test_delayed_directory_copy_preserves_known_deletion_and_adds_unknown_restored_file(self):
        repo, deleted, restored = self.restore_repository('delayed', delayed=True)
        self.check_delivery(repo.task(deleted + ',' + restored, '', multi=True), 'delayed-output',
            {'src/a.txt': b'A', 'good.txt': b'old good'},
            {'dst/a.txt': b'A', 'dst/b.txt': b'B', 'good.txt': b'new good'},
            {('R', 'dst/a.txt', 'src/a.txt'), ('A', 'dst/b.txt', ''), ('M', 'good.txt', '')})
