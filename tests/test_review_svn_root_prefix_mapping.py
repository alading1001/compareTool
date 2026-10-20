"""真实 SVN 历史根前缀映射，核对报告、端点字节及上线说明。"""
import json
import re
import shutil
import subprocess
from unittest import mock

from archive_workflow_fixtures import Repository, WorkflowCase
from report_generator import ReportGenerator
from test_review_five_integrity_fixes import delivered


class SVNRootPrefixMappingTests(WorkflowCase):
    def check_delivery(self, task, name, before, after, changes):
        app, result, out = self.generate(task, False, self.root / name)
        self.assert_success(app)
        self.assertEqual('completed', app._last_task_record['status'], result.issues)
        self.assertFalse(result.issues)
        prefix = task['project_name'] + '/'
        self.assertEqual({prefix + p: data for p, data in before.items()}, delivered(out, 'oldVersion'))
        self.assertEqual({prefix + p: data for p, data in after.items()}, delivered(out, 'newVersion'))
        self.assertEqual(changes, {(f.report_type, f.file_path, f.old_path)
                                   for f in result.report_manifest_files})
        self.assertEqual(len(changes), result.summary['total_files'])
        report = (out / 'report.html').read_text('utf-8')
        paths = {json.loads(value) for value in re.findall(
            r'^\s+path:\s+("(?:[^"\\]|\\.)*")', report, re.MULTILINE)}
        self.assertEqual({path for _kind, path, _old in changes}, paths)
        instructions = next(out.glob('*操作说明.txt')).read_text('utf-8-sig')
        self.assertNotIn('交付未完成', instructions)
        self.assert_no_stages(out)
        return result, out, instructions

    def nested_project(self, name, nested='sub'):
        repo = Repository(self, 'svn', name)
        first = repo.commit({'project/a.txt': b'unchanged root file\n',
                             f'project/{nested}/a.txt': b'old nested\n',
                             'project/good.txt': b'old good\n'})
        repo.cmd('delete', 'project/' + nested)
        repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / 'project' / nested))
        copied = repo.commit()
        final = repo.commit({f'project/{nested}/{nested}/a.txt': b'new nested\n',
                             f'project/{nested}/good.txt': b'new good\n'})
        return repo, first, copied, final, repo.path / 'project' / nested

    def root_copy_project(self, name, *, later_move=False):
        repo = Repository(self, 'svn', name)
        first = repo.commit({'a.txt': b'initial a\n', 'good.txt': b'initial good\n'})
        selected = repo.commit({'a.txt': b'selected a\n'})
        repo.cmd('copy', repo.url + '/@' + selected, repo.url + '/branch', '-m', 'copy root')
        repo.cmd('update')
        copied = repo.cmd('info', '--show-item', 'revision')
        final = repo.commit({'branch/a.txt': b'final a\n', 'branch/good.txt': b'final good\n'})
        scope = repo.path / 'branch'
        if later_move:
            repo.cmd('move', 'branch', 'destination')
            repo.commit()
            scope = repo.path / 'destination'
        return repo, first, selected, copied, final, scope

    def test_nested_old_and_new_roots_preserve_both_directions_and_literal_names(self):
        for index, nested in enumerate(('sub', 'sub # %20')):
            repo, first, copied, final, scope = self.nested_project('nested-' + str(index), nested)
            before = {nested + '/a.txt': b'old nested\n', 'good.txt': b'old good\n'}
            after = {nested + '/a.txt': b'new nested\n', 'good.txt': b'new good\n'}
            changes = {('M', path, '') for path in before}
            for name, old, new, expected_old, expected_new in (
                    ('forward', first, final, before, after),
                    ('reverse', final, first, after, before),
                    ('after-copy', copied, final, before, after)):
                with self.subTest(nested=nested, direction=name):
                    self.check_delivery(repo.task(old, new, scope=scope), f'{index}-{name}',
                                        expected_old, expected_new, changes)

    def test_nested_summary_applies_exclusion_and_directory_replacement_to_real_paths(self):
        repo = Repository(self, 'svn', 'nested-replacements')
        first = repo.commit({'project/sub/removed.txt': b'deleted body',
                             'project/sub/switch/child.txt': b'old child',
                             'project/sub/hidden.txt': b'old hidden'})
        repo.cmd('delete', 'project/sub')
        repo.cmd('copy', repo.url + '/project@' + first, str(repo.path / 'project/sub'))
        repo.commit()
        repo.cmd('delete', 'project/sub/sub/removed.txt', 'project/sub/sub/switch')
        final = repo.commit({'project/sub/sub/added.txt': b'added body',
                             'project/sub/sub/switch': b'new file',
                             'project/sub/sub/hidden.txt': b'new hidden'})
        task = repo.task(first, final, scope=repo.path / 'project/sub')
        task['exclude_rules'] = 'sub/hidden.txt'
        result, out, instructions = self.check_delivery(task, 'replacements',
            {'sub/removed.txt': b'deleted body', 'sub/switch/child.txt': b'old child'},
            {'sub/added.txt': b'added body', 'sub/switch': b'new file'},
            {('D', 'sub/removed.txt', ''), ('D', 'sub/switch/child.txt', ''),
             ('A', 'sub/added.txt', ''), ('A', 'sub/switch', '')})
        self.assertEqual(['sub/switch'], result.required_directory_deletions)
        self.assertIn('[删除文件] Demo/sub/removed.txt', instructions)
        self.assertIn('[删除旧目录后再写入同名文件] Demo/sub/switch', instructions)
        self.assertNotIn('hidden.txt', instructions)

    def test_multi_selection_crosses_root_copy_and_later_project_move(self):
        for moved in (False, True):
            repo, first, selected, copied, final, scope = self.root_copy_project(
                'root-copy-' + str(moved), later_move=moved)
            for name, versions, before, after, changes in (
                    ('before-copy', selected,
                     {'a.txt': b'initial a\n'}, {'a.txt': b'selected a\n'}, {('M', 'a.txt', '')}),
                    ('across-copy', selected + ',' + final,
                     {'a.txt': b'initial a\n', 'good.txt': b'initial good\n'},
                     {'a.txt': b'final a\n', 'good.txt': b'final good\n'},
                     {('M', 'a.txt', ''), ('M', 'good.txt', '')}),
                    ('copy-only', copied, {}, {}, set())):
                with self.subTest(moved=moved, selection=name):
                    self.check_delivery(repo.task(versions, '', multi=True, scope=scope),
                        f'{moved}-{name}', before, after, changes)

    def test_root_copy_preserves_file_properties_and_exclusion_snapshot(self):
        repo = Repository(self, 'svn', 'root-properties')
        repo.commit({'a.txt': b'initial\n', 'hidden.txt': b'initial hidden'})
        repo.cmd('propset', 'svn:eol-style', 'CRLF', 'a.txt')
        repo.cmd('propset', 'svn:executable', '*', 'a.txt')
        selected = repo.commit({'a.txt': b'selected\n', 'hidden.txt': b'selected hidden'})
        repo.cmd('copy', repo.url + '/@' + selected, repo.url + '/branch', '-m', 'copy root')
        repo.cmd('update')
        final = repo.commit({'branch/a.txt': b'final\n', 'branch/hidden.txt': b'final hidden'})
        task = repo.task(selected + ',' + final, '', multi=True, scope=repo.path / 'branch')
        task['exclude_rules'] = 'hidden.txt'
        result, out, instructions = self.check_delivery(task, 'root-properties-output',
            {'a.txt': b'initial\n'}, {'a.txt': b'final\r\n'}, {('M', 'a.txt', '')})
        self.assertFalse(result.files[0].old_executable)
        self.assertTrue(result.files[0].new_executable)
        self.assertIn('[设置可执行权限（chmod +x）] Demo/a.txt', instructions)
        self.assertNotIn('hidden.txt', instructions)

    def test_selected_root_copy_keeps_explicit_child_change(self):
        svnmucc = shutil.which('svnmucc')
        if not svnmucc:
            self.skipTest('需要 svnmucc 创建同 revision 根复制及子文件修改')
        repo = Repository(self, 'svn', 'root-child-change')
        before = repo.commit({'a.txt': b'before copy\n', 'good.txt': b'inherited\n'})
        payload = self.root / 'changed.txt'
        payload.write_bytes(b'changed during copy\n')
        subprocess.run([svnmucc, '--non-interactive', '-m', 'copy and edit',
                        'cp', before, repo.url + '/', repo.url + '/branch',
                        'put', str(payload), repo.url + '/branch/a.txt'],
                       cwd=repo.path, capture_output=True, check=True, timeout=60)
        repo.cmd('update')
        final = repo.cmd('info', '--show-item', 'revision')
        self.check_delivery(repo.task(final, '', multi=True, scope=repo.path / 'branch'),
            'child-change-output', {'a.txt': b'before copy\n'},
            {'a.txt': b'changed during copy\n'}, {('M', 'a.txt', '')})

    def test_mixed_multi_report_keeps_both_root_mappings(self):
        nested, first, _copied, final, nested_scope = self.nested_project('mixed-nested')
        root, _first, selected, _copied, last, root_scope = self.root_copy_project('mixed-root')
        tasks = [nested.task(first, final, scope=nested_scope, project_name='Nested'),
                 root.task(selected + ',' + last, '', multi=True, scope=root_scope, project_name='Root')]
        app = self.app()
        out = self.root / 'mixed'
        out.mkdir()
        captured = []
        original = ReportGenerator.generate_multi
        def observe(generator, results, *args, **kwargs):
            captured.extend(results)
            return original(generator, results, *args, **kwargs)
        with mock.patch.object(ReportGenerator, 'generate_multi', observe):
            app._do_generate_multi(tasks, str(out / 'report.html'),
                str(out / 'oldVersion'), str(out / 'newVersion'), str(out))
        self.assert_success(app)
        self.assertEqual('completed', app._last_task_record['status'])
        self.assertEqual(4, app._last_task_record['counts']['total_files'])
        self.assertTrue(all(not item['diff_result'].issues for item in captured))
        self.assertEqual({'Nested/sub/a.txt': b'old nested\n', 'Nested/good.txt': b'old good\n',
                          'Root/a.txt': b'initial a\n', 'Root/good.txt': b'initial good\n'},
                         delivered(out, 'oldVersion'))
        self.assertEqual({'Nested/sub/a.txt': b'new nested\n', 'Nested/good.txt': b'new good\n',
                          'Root/a.txt': b'final a\n', 'Root/good.txt': b'final good\n'},
                         delivered(out, 'newVersion'))
        self.assertEqual({'Nested': {('M', 'sub/a.txt', ''), ('M', 'good.txt', '')},
                          'Root': {('M', 'a.txt', ''), ('M', 'good.txt', '')}},
                         {item['project_name']: {(f.report_type, f.file_path, f.old_path)
                             for f in item['diff_result'].report_manifest_files} for item in captured})
        self.assertNotIn('交付未完成', (out / '上线操作说明.txt').read_text('utf-8-sig'))
        self.assert_no_stages(out)
