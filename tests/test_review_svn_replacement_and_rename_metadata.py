"""真实 SVN 源目录替换及重命名属性说明的交付回归。"""
from xml.etree import ElementTree

from archive_workflow_fixtures import Repository, WorkflowCase
from test_review_five_integrity_fixes import delivered


class SVNReplacedSourceTests(WorkflowCase):
    def moved_project(self, name, *, ancestor=True, replacement_file=False, later_move=False):
        repo = Repository(self, 'svn', name)
        source = 'container/project' if ancestor else 'project'
        root = source.split('/')[0]
        first = repo.commit({source + '/a.txt': b'first\n',
                             source + '/restored.txt': b'restored\n'})
        repo.cmd('delete', source + '/restored.txt')
        before = repo.commit({source + '/a.txt': b'second\n',
                              source + '/extra.txt': b'extra\n'})
        repo.cmd('copy', repo.url + '/' + source + '@' + first,
                 str(repo.path / 'destination'))
        repo.cmd('delete', root)
        replacement = root if replacement_file else root + '/unrelated.txt'
        restored = repo.commit({replacement: b'unrelated replacement\n'})
        log = ElementTree.fromstring(repo.cmd('log', '--xml', '-v', '-r', restored))
        replaced = next(p for p in log.findall('.//path') if p.text == '/' + root)
        self.assertEqual('R', replaced.get('action'))
        self.assertEqual('file' if replacement_file else 'dir', replaced.get('kind'))
        scope = repo.path / 'destination'
        if later_move:
            repo.cmd('move', 'destination', 'final')
            repo.commit()
            scope = repo.path / 'final'
        return repo, before, restored, scope

    def check_delivery(self, task, name, before, after, changes):
        app, result, out = self.generate(task, False, self.root / name)
        self.assert_success(app)
        self.assertEqual('completed', app._last_task_record['status'], result.issues)
        self.assertFalse(result.issues)
        self.assertEqual({'Demo/' + p: v for p, v in before.items()}, delivered(out, 'oldVersion'))
        self.assertEqual({'Demo/' + p: v for p, v in after.items()}, delivered(out, 'newVersion'))
        self.assertEqual(changes, {(f.report_type, f.file_path) for f in result.report_manifest_files})
        self.assertEqual(len(changes), app._last_task_record['counts']['total_files'])
        self.assertNotIn('交付未完成', next(out.glob('*操作说明.txt')).read_text('utf-8-sig'))
        self.assert_no_stages(out)
        return result, out

    def test_ordinary_replaced_source_and_ancestor_keep_real_endpoints_in_both_directions(self):
        for ancestor, replacement_file in ((False, False), (True, False), (True, True)):
            with self.subTest(ancestor=ancestor, replacement_file=replacement_file):
                label = f'{ancestor}-{replacement_file}'
                repo, before, restored, scope = self.moved_project(label,
                    ancestor=ancestor, replacement_file=replacement_file, later_move=True)
                old = {'a.txt': b'second\n', 'extra.txt': b'extra\n'}
                new = {'a.txt': b'first\n', 'restored.txt': b'restored\n'}
                for reverse in (False, True):
                    left, right = (restored, before) if reverse else (before, restored)
                    deleted, added = ('restored.txt', 'extra.txt') if reverse else ('extra.txt', 'restored.txt')
                    _result, out = self.check_delivery(repo.task(left, right, scope=scope),
                        label + '-' + str(reverse), new if reverse else old, old if reverse else new,
                        {('M', 'a.txt'), ('D', deleted), ('A', added)})
                    self.assertIn('[删除文件] Demo/' + deleted,
                                  next(out.glob('*操作说明.txt')).read_text('utf-8-sig'))

    def test_multi_replaced_ancestor_preserves_selected_history_restore_and_net_zero(self):
        for replacement_file in (False, True):
            with self.subTest(replacement_file=replacement_file):
                repo, before, restored, scope = self.moved_project(str(replacement_file),
                    replacement_file=replacement_file, later_move=True)
                initial = {'a.txt': b'first\n', 'restored.txt': b'restored\n'}
                changed = {'a.txt': b'second\n', 'extra.txt': b'extra\n'}
                cases = (
                    ('before', before, initial, changed,
                     {('M', 'a.txt'), ('D', 'restored.txt'), ('A', 'extra.txt')}),
                    ('restore', restored, changed, initial,
                     {('M', 'a.txt'), ('D', 'extra.txt'), ('A', 'restored.txt')}),
                    ('net-zero', before + ',' + restored, {}, {}, set()),
                )
                for name, versions, old, new, changes in cases:
                    self.check_delivery(repo.task(versions, '', multi=True, scope=scope),
                        str(replacement_file) + '-' + name, old, new, changes)


class SVNRenameMetadataTests(WorkflowCase):
    def renamed_project(self, name, change=None):
        repo = Repository(self, 'svn', name)
        repo.write({'before.txt': b'unchanged body\n'})
        repo.cmd('add', 'before.txt')
        repo.cmd('propset', 'svn:eol-style', 'LF', 'before.txt')
        repo.cmd('propset', 'svn:executable', '*', 'before.txt')
        repo.cmd('propset', 'custom:label', 'same', 'before.txt')
        first = repo.commit()
        repo.cmd('move', 'before.txt', 'after.txt')
        if change:
            repo.cmd(*change, 'after.txt')
        last = repo.commit()
        return repo, first, last

    def check_rename(self, name, change, expected, *, executable=True, new_bytes=b'unchanged body\n'):
        repo, first, last = self.renamed_project(name, change)
        app, result, out = self.generate(repo.task(first, last), False, self.root / (name + '-out'))
        self.assert_success(app)
        self.assertFalse(result.issues)
        self.assertEqual('completed', app._last_task_record['status'])
        self.assertEqual([('R', 'after.txt', 'before.txt')],
                         [(f.report_type, f.file_path, f.old_path) for f in result.report_manifest_files])
        file = result.files[0]
        self.assertEqual(expected, file.metadata_changes)
        self.assertTrue(file.old_executable)
        self.assertEqual(executable, file.new_executable)
        self.assertEqual({'Demo/before.txt': b'unchanged body\n'}, delivered(out, 'oldVersion'))
        self.assertEqual({'Demo/after.txt': new_bytes}, delivered(out, 'newVersion'))
        report = (out / 'report.html').read_text('utf-8')
        for note in expected:
            self.assertIn(note, report)
        for false_note in ('SVN 换行属性：LF → 未设置', 'SVN 换行属性：未设置 → LF'):
            if false_note not in expected:
                self.assertNotIn(false_note, report)
        self.assert_no_stages(out)
        return out

    def test_pure_rename_does_not_claim_unchanged_properties_were_removed_and_added(self):
        out = self.check_rename('unchanged', None, [])
        report = (out / 'report.html').read_text('utf-8')
        self.assertNotIn('SVN 可执行属性：', report)
        self.assertNotIn('SVN 其他属性变化', report)

    def test_rename_preserves_real_executable_removal_and_delivery_instruction(self):
        out = self.check_rename('executable', ('propdel', 'svn:executable'),
            ['SVN 可执行属性：已设置 → 未设置'], executable=False)
        self.assertIn('[移除可执行权限（chmod -x）] Demo/after.txt',
                      next(out.glob('*操作说明.txt')).read_text('utf-8-sig'))

    def test_rename_preserves_real_eol_property_removal(self):
        self.check_rename('eol', ('propdel', 'svn:eol-style'),
            ['SVN 换行属性：LF → 未设置'])

    def test_different_endpoint_bytes_keep_delete_add_and_correct_exports(self):
        repo, first, last = self.renamed_project('different-bytes',
            ('propset', 'svn:eol-style', 'CRLF'))
        app, result, out = self.generate(repo.task(first, last), False, self.root / 'different-bytes-out')
        self.assert_success(app)
        self.assertFalse(result.issues)
        self.assertEqual({('D', 'before.txt', ''), ('A', 'after.txt', '')},
            {(f.report_type, f.file_path, f.old_path) for f in result.report_manifest_files})
        self.assertEqual({'Demo/before.txt': b'unchanged body\n'}, delivered(out, 'oldVersion'))
        self.assertEqual({'Demo/after.txt': b'unchanged body\r\n'}, delivered(out, 'newVersion'))
        self.assert_no_stages(out)

    def test_rename_keeps_only_the_changed_custom_property(self):
        self.check_rename('custom', ('propset', 'custom:label', 'changed'),
            ['SVN 其他属性变化（不影响普通文件导出字节）：custom:label'])
