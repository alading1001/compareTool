"""同 revision 重命名、交付路径依赖、失效项目隔离和 worktree 分支回归。"""
from itertools import permutations
from pathlib import Path
import unittest
from unittest import mock

from archive_workflow_fixtures import WorkflowCase, Repository
from diff_engine import DiffResult, FileDiff
from file_exporter import FileExporter
from path_safety import TreePathCollision
import test_best_effort_export as best_effort
from vcs.base import ChangeType
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import _EndpointPlanner, _HistoryChange


class AtomicRenameTests(unittest.TestCase):
    def test_chains_and_cycles_do_not_depend_on_record_order(self):
        for moves in ((('a', 'b'), ('b', 'c')), (('a', 'b'), ('b', 'a'))):
            for primed in (False, True):
                for order in permutations(moves):
                    with self.subTest(moves=moves, primed=primed, order=order):
                        planner = _EndpointPlanner()
                        if primed:
                            planner.apply([_HistoryChange('M', p) for p in ('a', 'b')],
                                          False, '0', '1')
                        planner.apply([_HistoryChange('R', new, old) for old, new in order],
                                      True, '1', '2')
                        self.assertEqual(set(moves), {
                            (f.old_path, f.new_path) for f in planner.selected_entities})
        for order in permutations((('a', 'b'), ('b', 'c'))):
            with self.subTest(created_source=True, order=order):
                planner = _EndpointPlanner()
                planner.apply([_HistoryChange('A', 'a')] + [
                    _HistoryChange('R', new, old) for old, new in order], True, '1', '2')
                self.assertEqual({(None, 'b'), ('b', 'c')}, {
                    (f.old_path, f.new_path) for f in planner.selected_entities})


class SvnRenameDeliveryTests(WorkflowCase):
    def assert_chain_delivery(self, task, old, new):
        app, result, out = self.generate(task, False)
        self.assert_success(app)
        self.assertFalse(result.delivery_incomplete, result.issues)
        self.assertEqual({('a.txt', 'b.txt'), ('b.txt', 'c.txt')},
                         {(f.old_path, f.file_path) for f in result.files})
        for side, contents in (('oldVersion', old), ('newVersion', new)):
            self.assertEqual(contents, {p.relative_to(out/side/'Demo').as_posix(): p.read_bytes()
                for p in (out/side/'Demo').rglob('*') if p.is_file()})
        instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
        self.assertIn('Demo/a.txt\n  -> Demo/b.txt', instructions)
        self.assertIn('Demo/b.txt\n  -> Demo/c.txt', instructions)
        self.assertEqual(2, result.summary['renamed_files'])
        self.assert_no_stages(out)

    def test_two_distinct_files_moved_in_one_selected_revision(self):
        repo = Repository(self, 'svn', 'selected-chain')
        old = {'a.txt': b'original A\n', 'b.txt': b'original B\n'}
        repo.commit(old)
        repo.cmd('move', 'b.txt', 'c.txt')
        repo.cmd('move', 'a.txt', 'b.txt')
        last = repo.commit()
        self.assert_chain_delivery(repo.task(last, last, multi=True), old,
                                   {'b.txt': old['a.txt'], 'c.txt': old['b.txt']})

    def test_unselected_chain_keeps_each_files_selected_endpoints(self):
        repo = Repository(self, 'svn', 'unselected-chain')
        old = {'a.txt': b'original A\n', 'b.txt': b'original B\n'}
        repo.commit(old)
        first = repo.commit({'a.txt': b'selected A\n', 'b.txt': b'selected B\n'})
        repo.cmd('move', 'b.txt', 'c.txt')
        repo.cmd('move', 'a.txt', 'b.txt')
        repo.commit()
        new = {'b.txt': b'final A\n', 'c.txt': b'final B\n'}
        last = repo.commit(new)
        self.assert_chain_delivery(repo.task(first+','+last, last, multi=True), old, new)


class PartialPathDependencyTests(WorkflowCase):
    commit_index_tree = best_effort.BestEffortWorkflowTests.commit_index_tree

    def reuse_task(self, required_filter=False):
        repo = Repository(self, 'git', 'reuse')
        if required_filter:
            repo.cmd('config', 'filter.unavailable.required', 'true')
        attrs = {'.gitattributes': ('100644', b'b.txt filter=unavailable\n')}
        old = self.commit_index_tree(repo, dict(attrs, **{
            'a.txt': ('100644', b'important original A\n'*10),
            'good.txt': ('100644', b'old good')}))
        first = self.commit_index_tree(repo, dict(attrs, **{
            'b.txt': ('100644', b'important original A\n'*10),
            'good.txt': ('100644', b'old good')}), old)
        last = self.commit_index_tree(repo, dict(attrs, **{
            'a.txt': ('100644', b'unrelated new A\n'),
            'b.txt': ('100644', b'important original A\n'*10),
            'good.txt': ('100644', b'new good')}), first)
        return repo.task(first+','+last, last, multi=True)

    def assert_only_independent_delivery(self, task):
        app, result, out = self.generate(task, False)
        self.assert_success(app)
        self.assertEqual('partial', app._last_task_record['status'])
        self.assertEqual(['good.txt'], [f.file_path for f in result.files])
        for side, payload in (('oldVersion', b'old good'), ('newVersion', b'new good')):
            self.assertEqual({'Demo/good.txt': payload}, {
                p.relative_to(out/side).as_posix(): p.read_bytes()
                for p in (out/side).rglob('*') if p.is_file()})
        instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
        self.assertIn('交付未完成', instructions)
        self.assertNotIn('[重命名旧路径]', instructions)
        self.assertIn('a.txt', instructions)
        self.assert_no_stages(out)

    def test_endpoint_failure_blocks_reused_old_path(self):
        self.assert_only_independent_delivery(self.reuse_task(required_filter=True))

    def test_export_failure_blocks_reused_old_path(self):
        task = self.reuse_task()
        original = FileExporter._write_file
        def fail(exporter, base, path, version, content):
            if path == 'b.txt':
                raise OSError('injected endpoint write failure')
            return original(exporter, base, path, version, content)
        with mock.patch.object(FileExporter, '_write_file', fail):
            self.assert_only_independent_delivery(task)

    def test_successful_reuse_still_exports_all_endpoints(self):
        app, result, out = self.generate(self.reuse_task(), False)
        self.assert_success(app)
        self.assertFalse(result.delivery_incomplete, result.issues)
        self.assertEqual({'a.txt', 'b.txt', 'good.txt'}, {f.file_path for f in result.files})
        self.assertEqual(b'important original A\n'*10, (out/'oldVersion/Demo/a.txt').read_bytes())
        self.assertEqual(b'important original A\n'*10, (out/'newVersion/Demo/b.txt').read_bytes())
        self.assertEqual(b'unrelated new A\n', (out/'newVersion/Demo/a.txt').read_bytes())

    def test_dependency_propagates_through_same_path_and_directory_groups(self):
        # 失败项已在前阶段移除，仍须阻断其旧路径复用及跨目录替换的依赖。
        result = DiffResult('', 'Demo', 'Git', 'old', 'new', files=[
            FileDiff('node', ChangeType.RENAMED, old_path='A.txt'),
            FileDiff('elsewhere.txt', ChangeType.RENAMED, old_path='node/child.txt'),
            FileDiff('good.txt', ChangeType.ADDED)], replacement_dependency_roots=['node'])
        result.issues.append(dict(stage='文件端点', path='failed.txt', old_path='a.TXT',
                                  message='read failed', delivery=True))
        vcs = mock.Mock(spec=['get_file_content_bytes'])
        vcs.get_file_content_bytes.return_value = b'validated endpoint'
        FileExporter(result, vcs, best_effort=True).export(str(self.root/'old'), str(self.root/'new'))
        self.assertEqual(['good.txt'], [f.file_path for f in result.files])
        self.assertFalse(any(p.is_file() for p in (self.root/'old').rglob('*')))
        self.assertEqual(['good.txt'], [p.name for p in (self.root/'new').rglob('*') if p.is_file()])

    def test_collision_invalidates_delivered_rename_and_retains_its_old_path(self):
        result = DiffResult('', 'Demo', 'Git', 'old', 'new', files=[
            FileDiff('a.txt', ChangeType.ADDED),
            FileDiff('b.txt', ChangeType.RENAMED, old_path='a.txt'),
            FileDiff('alias.txt', ChangeType.ADDED),
            FileDiff('good.txt', ChangeType.ADDED)])
        vcs = mock.Mock(spec=['get_file_content_bytes'])
        vcs.get_file_content_bytes.return_value = b'validated endpoint'
        original = FileExporter._write_file
        def collide(exporter, base, path, version, content):
            if path == 'alias.txt':
                raise TreePathCollision('injected actual-name collision', str(Path(base)/'b.txt'))
            return original(exporter, base, path, version, content)
        with mock.patch.object(FileExporter, '_write_file', collide):
            FileExporter(result, vcs, best_effort=True).export(str(self.root/'old'), str(self.root/'new'))
        issue = next(i for i in result.issues if i['path'] == 'b.txt')
        self.assertEqual('a.txt', issue.get('old_path'))
        self.assertEqual(['good.txt'], [f.file_path for f in result.files])
        self.assertFalse(any(p.is_file() for p in (self.root/'old').rglob('*')))
        self.assertEqual(['good.txt'], [p.name for p in (self.root/'new').rglob('*') if p.is_file()])


class MissingGitProjectTests(WorkflowCase):
    def tasks(self, kind='git', missing=None):
        old, new = self.root/'old', self.root/'new'
        old.mkdir(exist_ok=True); new.mkdir(exist_ok=True)
        (old/'good.txt').write_bytes(b'old'); (new/'good.txt').write_bytes(b'new')
        good = dict(vcs_type='folder', project_name='Healthy', project_path='',
                    old_version=str(old), new_version=str(new), exclude_rules='')
        bad = dict(vcs_type=kind, project_name='Missing',
                   project_path=str(missing or self.root/'moved-away'),
                   old_version='1', new_version='2', exclude_rules='')
        return [good, bad]

    def test_worker_retains_missing_project_and_delivers_healthy_project(self):
        for kind in ('git', 'git_multi'):
            with self.subTest(kind=kind):
                out = self.root/(kind+'-output'); out.mkdir()
                app = self.app()
                app._do_generate_multi(self.tasks(kind), str(out/'report.html'),
                    str(out/'oldVersion'), str(out/'newVersion'), str(out))
                self.assert_success(app)
                self.assertEqual('partial', app._last_task_record['status'])
                self.assertEqual(b'new', (out/'newVersion/Healthy/good.txt').read_bytes())
                for path in (out/'report.html', out/'上线操作说明.txt'):
                    text = path.read_text(encoding='utf-8-sig')
                    self.assertIn('Missing', text)
                    self.assertIn('未完成', text)
                    self.assertIn('moved-away', text)
                self.assert_no_stages(out)

    def test_gui_preflight_allows_external_output_with_missing_git_project(self):
        app = self.app(); out = self.root/'gui-output'
        app._multi_tasks = self.tasks()
        app.output_dir_var = mock.Mock(); app.output_dir_var.get.return_value = str(out)
        app.old_export_var = mock.Mock(); app.new_export_var = mock.Mock()
        app._multi_run_paths = mock.Mock(return_value=tuple(map(str, (
            out, out/'report.html', out/'oldVersion', out/'newVersion'))))
        for name in ('_save_current_exclude_rules_for_current_key', '_save_current_config',
                     '_set_generating', 'progress', 'status_var'):
            setattr(app, name, mock.Mock())
        app._confirm_output_batch = mock.Mock(return_value=True)
        # 替换 main 的模块引用，不修改 subprocess 读取管道也会使用的全局 Thread。
        with mock.patch('main.threading') as threading, mock.patch('main.messagebox.showwarning') as warning:
            app._generate_multi()
        warning.assert_not_called()
        threading.Thread.return_value.start.assert_called_once()

    def test_missing_subproject_cannot_bypass_repository_output_protection(self):
        repo = Repository(self, 'git', 'parent-repo')
        repo.commit({'delivery/oldVersion/keep.txt': b'protected source'})
        for output in (repo.path/'delivery', repo.path/'.git'/'delivery'):
            with self.subTest(output=output):
                app = self.app()
                with mock.patch.object(FileExporter, 'output_session') as session:
                    app._do_generate_multi(self.tasks(missing=repo.path/'missing-component'),
                        str(output/'report.html'), str(output/'oldVersion'),
                        str(output/'newVersion'), str(output))
                self.assertFalse(app._last_task_record['success'])
                session.assert_not_called()
        self.assertEqual(b'protected source', (repo.path/'delivery/oldVersion/keep.txt').read_bytes())

    def test_missing_project_cannot_be_recreated_as_output(self):
        missing = self.root/'missing-project'; app = self.app()
        with mock.patch.object(FileExporter, 'output_session') as session:
            app._do_generate_multi(self.tasks(missing=missing), str(missing/'report.html'),
                str(missing/'oldVersion'), str(missing/'newVersion'), str(self.root))
        self.assertFalse(app._last_task_record['success'])
        session.assert_not_called()
        self.assertFalse(missing.exists())


class GitVersionListTests(WorkflowCase):
    def test_worktree_branch_fills_valid_name_and_detached_head_is_not_a_branch(self):
        repo = Repository(self, 'git', 'branches'); repo.commit({'a.txt': b'example'})
        repo.cmd('worktree', 'add', '-b', 'review-feature', str(self.root/'other-worktree'), 'HEAD')
        repo.cmd('tag', 'review-tag')
        for detached in (False, True):
            with self.subTest(detached=detached):
                if detached:
                    repo.cmd('checkout', '--detach', 'HEAD')
                vcs = GitVCS(str(repo.path))
                self.addCleanup(vcs.cleanup)
                versions = vcs.get_versions()
                branch = next(item for item in versions if 'review-feature' in item)
                self.assertEqual('review-feature', branch)
                self.assertFalse(any(item.startswith('(') for item in versions))
                self.assertIn('review-tag', versions)
                app = self.app(); app.vcs_var = mock.Mock(); app.vcs_var.get.return_value = 'git'
                app.version_listbox = mock.Mock(); app.version_listbox.curselection.return_value = (0,)
                app.version_listbox.get.return_value = branch
                app.new_version_var = mock.Mock(); app._version_target = 'new'
                app._fill_selected_version()
                app.new_version_var.set.assert_called_once_with('review-feature')
                self.assertTrue(vcs.check_version_exists(branch))


if __name__ == '__main__':
    unittest.main()
