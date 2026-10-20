"""真实 SVN 路径、同名替换及桌面输出入口的交付回归。"""
import shutil
import subprocess
import urllib.request
from pathlib import Path
from unittest import mock

from archive_workflow_fixtures import Repository, WorkflowCase
from main import CompareToolApp
from vcs.base import ChangeType
from vcs.multi_version_vcs import SVNMultiVersionVCS
from vcs.svn_vcs import SVNVCS


class SVNReviewDeliveryTests(WorkflowCase):
    def transaction(self, repo, *actions):
        executable = shutil.which('svnmucc')
        if not executable:
            self.skipTest('需要 svnmucc 创建 Windows 无法检出的 SVN 字面名称')
        subprocess.run([executable, '--non-interactive', '-m', 'path fixture', *actions],
                       cwd=repo.path, capture_output=True, check=True, timeout=60)
        return repo.cmd('info', '--show-item', 'revision', '-r', 'HEAD', repo.url + '@HEAD')

    def payload(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    def delivered(self, output, side):
        root = output / side / 'Demo'
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob('*') if p.is_file()}

    def test_literal_backslash_add_modify_delete_never_aliases_slash_file(self):
        repo = Repository(self, 'svn', 'literal')
        base = repo.commit({'a/b.txt': b'unchanged slash\n', 'good.py': b'zero\n'})
        bad_url = repo.url + '/a%5Cb.txt'
        one = self.transaction(repo, 'put', self.payload('one', b'literal one\n'), bad_url,
                               'put', self.payload('good-one', b'one\n'), repo.url + '/good.py')
        two = self.transaction(repo, 'put', self.payload('two', b'literal two\n'), bad_url,
                               'put', self.payload('good-two', b'two\n'), repo.url + '/good.py')
        three = self.transaction(repo, 'rm', bad_url,
                                 'put', self.payload('good-three', b'three\n'), repo.url + '/good.py')
        for index, (old, new, before, after) in enumerate((
                (base, one, b'zero\n', b'one\n'),
                (one, two, b'one\n', b'two\n'),
                (two, three, b'two\n', b'three\n'))):
            with self.subTest(index=index, api='normal'):
                vcs = SVNVCS(str(repo.path))
                try:
                    with self.assertRaisesRegex(RuntimeError, '字面反斜杠'):
                        vcs.get_changed_files(old, new)
                finally:
                    vcs.cleanup()
            with self.subTest(index=index, api='multi'):
                with self.assertRaisesRegex(RuntimeError, '字面反斜杠'):
                    SVNMultiVersionVCS(str(repo.path), [new])
            for multi in (False, True):
                task = repo.task(new if multi else old, new, multi=multi)
                with self.subTest(index=index, multi=multi):
                    app, result, output = self.generate(task, False, self.root / f'literal-{index}-{multi}')
                    self.assert_success(app)
                    self.assertEqual('partial', app._last_task_record['status'])
                    self.assertEqual({'good.py'}, {f.file_path for f in result.files})
                    self.assertEqual({'good.py': before}, self.delivered(output, 'oldVersion'))
                    self.assertEqual({'good.py': after}, self.delivered(output, 'newVersion'))
                    self.assertTrue(any(i['path'] == r'a\b.txt' and i['delivery'] for i in result.issues))
                    instructions = next(output.glob('*操作说明.txt')).read_text('utf-8-sig')
                    self.assertIn('交付未完成', instructions)
                    self.assertNotIn('[删除文件] Demo/a/b.txt', instructions)
                    self.assertIn('字面反斜杠', (output / 'report.html').read_text('utf-8'))
                    self.assert_no_stages(output)
        # 原始读取仍能准确寻址仓库名称，不能读取斜杠同名文件。
        vcs = SVNVCS(str(repo.path))
        try:
            self.assertEqual(b'literal two\n', vcs.get_file_content_raw_bytes(two, r'a\b.txt'))
        finally:
            vcs.cleanup()

    def test_exclusion_uses_literal_hierarchy_and_keeps_valid_slash_change(self):
        repo = Repository(self, 'svn', 'literal-exclusion')
        old = repo.commit({'a/b.txt': b'old slash', 'good.py': b'old good'})
        new = self.transaction(repo,
            'put', self.payload('bad', b'bad name'), repo.url + '/a%5Cb.txt',
            'put', self.payload('slash', b'new slash'), repo.url + '/a/b.txt',
            'put', self.payload('good', b'new good'), repo.url + '/good.py')
        for multi in (False, True):
            for index, (rule, slash, partial) in enumerate((('', True, True),
                    ('a/b.txt', False, True), ('*b.txt', False, False))):
                with self.subTest(multi=multi, rule=rule):
                    task = repo.task(new if multi else old, new, multi=multi)
                    task['exclude_rules'] = rule
                    app, result, output = self.generate(task, False, self.root / f'exclude-{multi}-{index}')
                    self.assert_success(app)
                    self.assertEqual(partial, result.delivery_incomplete, result.issues)
                    before, after = {'good.py': b'old good'}, {'good.py': b'new good'}
                    if slash:
                        before['a/b.txt'], after['a/b.txt'] = b'old slash', b'new slash'
                    self.assertEqual(before, self.delivered(output, 'oldVersion'))
                    self.assertEqual(after, self.delivered(output, 'newVersion'))

    def test_intermediate_literal_directory_move_keeps_legal_final_endpoints(self):
        repo = Repository(self, 'svn', 'intermediate')
        base = repo.commit({'src/file.txt': b'preserved contents'})
        middle = self.transaction(repo, 'cp', base, repo.url + '/src', repo.url + '/bad%5Cdir',
                                  'rm', repo.url + '/src')
        final = self.transaction(repo, 'cp', middle, repo.url + '/bad%5Cdir', repo.url + '/done',
                                 'rm', repo.url + '/bad%5Cdir')
        task = repo.task(middle + ',' + final, '', multi=True)
        app, result, output = self.generate(task, False, self.root / 'intermediate-output')
        self.assert_success(app)
        self.assertFalse(result.delivery_incomplete, result.issues)
        self.assertEqual({('src/file.txt', 'done/file.txt')},
                         {(f.old_path, f.file_path) for f in result.files})
        self.assertEqual({'src/file.txt': b'preserved contents'}, self.delivered(output, 'oldVersion'))
        self.assertEqual({'done/file.txt': b'preserved contents'}, self.delivered(output, 'newVersion'))

    def test_literal_directory_exclusion_uses_real_hierarchy_and_final_files(self):
        repo = Repository(self, 'svn', 'directory-exclusion')
        old = repo.commit({'bad/dir/real.txt': b'stable', 'good.py': b'old good'})
        new = self.transaction(repo, 'mkdir', repo.url + '/bad%5Cdir',
            'put', self.payload('directory-bad', b'literal'), repo.url + '/bad%5Cdir/skip.txt',
            'put', self.payload('directory-good', b'new good'), repo.url + '/good.py')
        for multi in (False, True):
            for index, (rule, partial) in enumerate((('*.txt', False), ('bad/dir/**', True))):
                with self.subTest(multi=multi, rule=rule):
                    task = repo.task(new if multi else old, new, multi=multi)
                    task['exclude_rules'] = rule
                    app, result, output = self.generate(task, False,
                        self.root / f'directory-exclude-{multi}-{index}')
                    self.assert_success(app)
                    self.assertEqual(partial, result.delivery_incomplete, result.issues)
                    self.assertEqual({'good.py': b'old good'}, self.delivered(output, 'oldVersion'))
                    self.assertEqual({'good.py': b'new good'}, self.delivered(output, 'newVersion'))

    def test_directory_listing_preserves_literal_member_names(self):
        repo = Repository(self, 'svn', 'listing')
        base = repo.commit({'src/a/b.txt': b'unchanged slash', 'src/good.py': b'good'})
        added = self.transaction(repo, 'put', self.payload('listed-bad', b'literal'),
                                 repo.url + '/src/a%5Cb.txt')
        moved = self.transaction(repo, 'cp', added, repo.url + '/src', repo.url + '/dst',
                                 'rm', repo.url + '/src')
        app, result, output = self.generate(repo.task(moved, '', multi=True), False,
                                            self.root / 'listing-output')
        self.assert_success(app)
        self.assertTrue(result.delivery_incomplete)
        self.assertEqual({'src/a/b.txt': b'unchanged slash', 'src/good.py': b'good'},
                         self.delivered(output, 'oldVersion'))
        self.assertEqual({'dst/a/b.txt': b'unchanged slash', 'dst/good.py': b'good'},
                         self.delivered(output, 'newVersion'))
        self.assertTrue(any(r'a\b.txt' in i['path'] for i in result.issues))

    def test_same_path_replacement_is_modification_with_real_endpoint_metadata(self):
        repo = Repository(self, 'svn', 'replacement')
        old = repo.commit({'same.txt': b'original\n', 'old.txt': b'renamed\n'})
        repo.cmd('delete', 'same.txt')
        repo.write({'same.txt': b'original\n'})
        repo.cmd('add', 'same.txt')
        repo.cmd('propset', 'svn:executable', '*', 'same.txt')
        repo.cmd('move', 'old.txt', 'new.txt')
        new = repo.commit()
        app, result, output = self.generate(repo.task(old, new), False, self.root / 'replacement-output')
        self.assert_success(app)
        by_path = {f.file_path: f for f in result.files}
        self.assertEqual(ChangeType.MODIFIED, by_path['same.txt'].change_type)
        self.assertEqual('', by_path['same.txt'].old_path)
        self.assertFalse(by_path['same.txt'].old_executable)
        self.assertTrue(by_path['same.txt'].new_executable)
        self.assertEqual(['SVN 可执行属性：未设置 → 已设置'], by_path['same.txt'].metadata_changes)
        self.assertEqual(ChangeType.RENAMED, by_path['new.txt'].change_type)
        self.assertEqual('old.txt', by_path['new.txt'].old_path)
        self.assertEqual({'same.txt': b'original\n', 'old.txt': b'renamed\n'}, self.delivered(output, 'oldVersion'))
        self.assertEqual({'same.txt': b'original\n', 'new.txt': b'renamed\n'}, self.delivered(output, 'newVersion'))
        instructions = next(output.glob('*操作说明.txt')).read_text('utf-8-sig')
        self.assertNotIn('[重命名旧路径] Demo/same.txt', instructions)
        self.assertNotIn('[删除文件] Demo/same.txt', instructions)
        self.assertIn('chmod +x', instructions)
        self.assertIn('[重命名旧路径] Demo/old.txt', instructions)

    def test_same_path_replacement_with_changed_bytes_keeps_both_endpoints(self):
        repo = Repository(self, 'svn', 'replacement-content')
        old = repo.commit({'same.txt': b'before\n'})
        repo.cmd('delete', 'same.txt')
        repo.write({'same.txt': b'after\n'})
        repo.cmd('add', 'same.txt')
        new = repo.commit()
        app, result, output = self.generate(repo.task(old, new), False, self.root / 'replacement-content-output')
        self.assert_success(app)
        self.assertEqual([(ChangeType.MODIFIED, 'same.txt', '')],
                         [(f.change_type, f.file_path, f.old_path) for f in result.files])
        self.assertEqual({'same.txt': b'before\n'}, self.delivered(output, 'oldVersion'))
        self.assertEqual({'same.txt': b'after\n'}, self.delivered(output, 'newVersion'))
        self.assertIn('删除文件：0 个', next(output.glob('*操作说明.txt')).read_text('utf-8-sig'))

    def test_svn_at_output_allows_untracked_but_rejects_versioned_paths(self):
        repo = Repository(self, 'svn', 'at-output')
        repo.commit({'source.txt': b'versioned'})
        (repo.path / 'tracked@review').mkdir()
        (repo.path / 'tracked@review/source.txt').write_bytes(b'protected')
        repo.cmd('add', 'tracked@review@')
        repo.cmd('commit', '-m', 'tracked at path')
        untracked = repo.path / 'reports@review'
        untracked.mkdir()
        (untracked / 'report.html').write_text('prior report', encoding='utf8')
        for mode in ('svn', 'svn_multi'):
            with self.subTest(mode=mode):
                CompareToolApp._validate_repository_output_targets(str(repo.path), mode,
                    [str(untracked / 'report.html'), str(untracked / 'newVersion')])
                with self.assertRaisesRegex(ValueError, 'SVN 已版本化'):
                    CompareToolApp._validate_repository_output_targets(str(repo.path), mode,
                        [str(repo.path / 'tracked@review')])
                self.assertEqual(b'protected', (repo.path / 'tracked@review/source.txt').read_bytes())


class ReportOpeningTests(WorkflowCase):
    def test_single_and_multi_completion_open_literal_file_urls(self):
        summary = dict(total_files=1, total_added_lines=1, total_deleted_lines=0, project_count=1)
        for callback in ('_on_complete', '_on_multi_complete'):
            for name in ('report.html', 'report#literal.html', 'report%20literal.html'):
                with self.subTest(callback=callback, name=name):
                    output = self.root / '输出 # literal %20'
                    output.mkdir(exist_ok=True)
                    report = output / name
                    report.write_bytes(b'<html>the intended report</html>')
                    app = CompareToolApp.__new__(CompareToolApp)
                    for attr in ('progress', '_set_generating', '_refresh_recent_project_values', 'status_var'):
                        setattr(app, attr, mock.Mock())
                    app._task_status_suffix = lambda: ''
                    app._task_details_note = lambda: ''
                    with mock.patch('main.messagebox.askyesno', return_value=True), \
                            mock.patch('main.webbrowser.open') as opened:
                        getattr(CompareToolApp, callback)(app, str(report), summary)
                    # 读取回调实际传出的 URL，而不是只断言某个编码实现。
                    with urllib.request.urlopen(opened.call_args.args[0]) as response:
                        self.assertEqual(report.read_bytes(), response.read())


if __name__ == '__main__':
    import unittest
    unittest.main()
