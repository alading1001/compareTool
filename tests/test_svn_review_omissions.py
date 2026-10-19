"""SVN omissions must be visible in reports, exported bytes and deployment steps."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from urllib.parse import quote

from archive_workflow_fixtures import Repository, WorkflowCase
from vcs.svn_vcs import SVNVCS


class SVNDeliveryOmissionTests(WorkflowCase):
    def check_delivery(self, task, name, old, new, changes):
        app, result, output = self.generate(task, False, self.root / name)
        self.assert_success(app)
        self.assertEqual('completed', app._last_task_record['status'])
        self.assertEqual([], result.issues)
        for side, expected in (('oldVersion', old), ('newVersion', new)):
            root = output / side / 'Demo'
            actual = {p.relative_to(root).as_posix(): p.read_bytes()
                      for p in root.rglob('*') if p.is_file()}
            self.assertEqual(expected, actual)
        self.assertEqual(changes, {(f.report_type, f.file_path, f.old_path)
                                   for f in result.files})
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
        self.assert_no_stages(output)
        return instructions

    def test_stale_directory_copy_keeps_real_source_deletions_and_net_zero(self):
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                repo = Repository(self, 'svn', 'stale-' + str(ancestor))
                source = 'parent/src' if ancestor else 'src'
                base_files = {source + '/a.txt': b'original A', 'good.txt': b'old good'}
                if ancestor:
                    base_files['parent/sibling.txt'] = b'sibling'
                original = repo.commit(base_files)
                before = repo.commit({source + '/b.txt': b'added after copy source'})
                repo.cmd('copy', repo.url + '/' + source + '@' + original,
                         str(repo.path / 'dst'))
                repo.cmd('delete', 'parent' if ancestor else source)
                moved = repo.commit({'good.txt': b'new good'})
                for both in (False, True):
                    for patterns in ('', 'dst/**', source + '/b.txt'):
                        with self.subTest(both=both, patterns=patterns):
                            selected = before + ',' + moved if both else moved
                            task = repo.task(selected, '', multi=True)
                            task['exclude_rules'] = patterns
                            old = {source + '/a.txt': b'original A', 'good.txt': b'old good'}
                            new = {'dst/a.txt': b'original A', 'good.txt': b'new good'}
                            changes = {('R', 'dst/a.txt', source + '/a.txt'), ('M', 'good.txt', '')}
                            if ancestor:
                                old['parent/sibling.txt'] = b'sibling'
                                changes.add(('D', 'parent/sibling.txt', ''))
                            if not both and patterns != source + '/b.txt':
                                old[source + '/b.txt'] = b'added after copy source'
                                changes.add(('D', source + '/b.txt', ''))
                            if patterns == 'dst/**':
                                new.pop('dst/a.txt')
                                changes.remove(('R', 'dst/a.txt', source + '/a.txt'))
                                changes.add(('D', source + '/a.txt', ''))
                            name = f'stale-{ancestor}-{both}-{len(patterns)}'
                            self.check_delivery(task, name, old, new, changes)

    def test_whitespace_only_names_are_added_modified_and_deleted(self):
        repo = Repository(self, 'svn', 'whitespace')
        name, added = '\u3000', '\u00a0'
        base = repo.commit({name: b'before', 'good.txt': b'old good'})
        changed = repo.commit({name: b'after', added: b'added', 'good.txt': b'new good'})
        repo.cmd('delete', repo.url + '/' + quote(name), '-m', 'delete whitespace file')
        repo.cmd('update')
        deleted = repo.cmd('info', '--show-item', 'revision')
        cases = (
            (base, changed, {name: b'before', 'good.txt': b'old good'},
             {name: b'after', added: b'added', 'good.txt': b'new good'},
             {('M', name, ''), ('A', added, ''), ('M', 'good.txt', '')}),
            (changed, deleted, {name: b'after'}, {}, {('D', name, '')}),
        )
        for index, (old_rev, new_rev, old, new, changes) in enumerate(cases):
            for multi in (False, True):
                with self.subTest(index=index, multi=multi):
                    task = repo.task(new_rev if multi else old_rev, new_rev, multi=multi)
                    self.check_delivery(task, f'whitespace-{index}-{multi}', old, new, changes)

    def test_whitespace_only_file_replaced_by_directory_keeps_old_file(self):
        svnmucc = shutil.which('svnmucc')
        if not svnmucc:
            self.skipTest('需要 svnmucc 原子替换本地测试仓库中的 Unicode 路径')
        repo = Repository(self, 'svn', 'whitespace-directory')
        name = '\u3000'
        base = repo.commit({name: b'old file'})
        # A URL transaction avoids Windows SVN's Unicode working-copy argv conversion.
        payload = self.root / 'child.txt'
        payload.write_bytes(b'new child')
        url = repo.url + '/' + quote(name)
        subprocess.run([svnmucc, '--non-interactive', '-m', 'replace whitespace file',
                        'rm', url, 'mkdir', url, 'put', str(payload), url + '/child.txt'],
                       cwd=repo.path, capture_output=True, check=True, timeout=60)
        repo.cmd('update')
        changed = repo.cmd('info', '--show-item', 'revision')
        changes = {('D', name, ''), ('A', name + '/child.txt', '')}
        for multi in (False, True):
            with self.subTest(multi=multi):
                self.check_delivery(repo.task(changed if multi else base, changed, multi=multi),
                    'whitespace-directory-' + str(multi), {name: b'old file'},
                    {name + '/child.txt': b'new child'}, changes)


class SVNSummaryLiteralPathTests(unittest.TestCase):
    def test_url_relative_and_absolute_summary_paths_preserve_whitespace(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs.project_path = str(Path('.').resolve())
        vcs._project_url_at = lambda revision: 'https://svn.invalid/project'
        for name in ('\u3000', '\u00a0', 'a.txt\u3000'):
            for raw in (name, vcs.project_path + '/' + name,
                        'https://svn.invalid/project/' + quote(name)):
                with self.subTest(raw=raw):
                    self.assertEqual(name, vcs._summary_relative_path(raw, '1', '2'))


if __name__ == '__main__':
    unittest.main()
