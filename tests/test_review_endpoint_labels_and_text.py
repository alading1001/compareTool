"""原始 Git 路径、快照侧别、SOH 展示及损坏缓存标记的交付回归。"""
import json
import os
from pathlib import Path
import subprocess
import time
from unittest import mock

from archive_workflow_fixtures import WorkflowCase, Repository, package
from archive_report import enrich_archive_reports
from diff_engine import DiffEngine
from file_exporter import FileExporter
from html_details import HtmlDetailStore
from report_generator import ReportGenerator
from test_complete_export_review_fixes import BytesVCS, TableRows
from vcs.base import ChangeType
from vcs.folder_vcs import FolderVCS
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS
from vcs import temp_storage


def commit_tree(repo, files, parent=None):
    """直接创建真实 Git 树，保留 Windows 无法检出的字面反斜杠名称。"""
    def command(args, data):
        return subprocess.run([repo.executable, *args], cwd=repo.path, input=data,
                              capture_output=True, check=True).stdout.strip()
    tree = {}
    for path, data in files.items():
        node = tree
        parts = path.split('/')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data
    def write_tree(node):
        records = []
        for name, data in node.items():
            if isinstance(data, dict):
                mode, kind, oid = b'040000', b'tree', write_tree(data)
            else:
                mode, kind, oid = b'100644', b'blob', command(['hash-object', '-w', '--stdin'], data)
            records.append(mode + b' ' + kind + b' ' + oid + b'\t' + name.encode() + b'\0')
        return command(['mktree', '-z'], b''.join(records))
    args = ['commit-tree', write_tree(tree).decode(), '-m', 'path fixture']
    if parent:
        args += ['-p', parent]
    revision = repo.cmd(*args)
    repo.cmd('update-ref', 'HEAD', revision)
    return revision


class GitLiteralPathTests(WorkflowCase):
    def assert_delivered(self, result, out, old, new, partial):
        self.assertEqual(partial, result.delivery_incomplete, result.issues)
        for side, contents in (('oldVersion', old), ('newVersion', new)):
            self.assertEqual(contents, {p.relative_to(out/side/'Demo').as_posix(): p.read_bytes()
                for p in (out/side/'Demo').rglob('*') if p.is_file()})
        self.assert_no_stages(out)

    def test_literal_backslash_cannot_alias_a_real_directory_or_its_exclusion(self):
        repo = Repository(self, 'git', 'literal')
        before = {r'a\b.txt': b'literal old\n', 'a/b.txt': b'slash old\n', 'good.txt': b'old\n'}
        after = {r'a\b.txt': b'literal new\n', 'a/b.txt': b'slash new\n', 'good.txt': b'new\n'}
        old = commit_tree(repo, before)
        new = commit_tree(repo, after, old)
        vcs = GitVCS(str(repo.path)); self.addCleanup(vcs.cleanup)
        # 原始读取必须得到 Git 自己的文件，不能读取它的斜杠同名文件。
        self.assertEqual(before[r'a\b.txt'], vcs.get_file_content_raw_bytes(old, r'a\b.txt'))
        self.assertEqual(after['a/b.txt'], vcs.get_file_content_raw_bytes(new, 'a/b.txt'))
        with self.assertRaisesRegex(RuntimeError, '字面反斜杠'):
            vcs.get_changed_files(old, new)
        with self.assertRaisesRegex(RuntimeError, '字面反斜杠'):
            GitMultiVersionVCS(str(repo.path), [new])
        for multi in (False, True):
            for exclude in ('', 'a/b.txt'):
                with self.subTest(multi=multi, exclude=exclude):
                    task = repo.task(new if multi else old, new, multi=multi)
                    task['exclude_rules'] = exclude
                    app, result, out = self.generate(task, False, self.root/f'out-{multi}-{bool(exclude)}')
                    self.assert_success(app)
                    self.assertEqual('partial', app._last_task_record['status'])
                    self.assertTrue(any(i['path'] == r'a\b.txt' and i['delivery'] for i in result.issues))
                    wanted = ['good.txt'] if exclude else ['a/b.txt', 'good.txt']
                    self.assertEqual(set(wanted), {f.file_path for f in result.files})
                    self.assert_delivered(result, out, {p: before[p] for p in wanted},
                                          {p: after[p] for p in wanted}, True)
                    report = (out/'report.html').read_text('utf-8')
                    instructions = next(out.glob('*操作说明.txt')).read_text('utf-8-sig')
                    self.assertIn('字面反斜杠', report)
                    self.assertIn('交付未完成', instructions)

    def test_unselected_backslash_rename_keeps_valid_final_identity(self):
        repo = Repository(self, 'git', 'intermediate')
        innocent = {'a/b.txt': b'independent\n'}
        base = commit_tree(repo, dict(innocent, **{'start.txt': b'original\n' * 10}))
        first = commit_tree(repo, dict(innocent, **{'start.txt': b'selected\n' * 10}), base)
        middle = commit_tree(repo, dict(innocent, **{r'a\b.txt': b'selected\n' * 10}), first)
        moved = commit_tree(repo, dict(innocent, **{'end.txt': b'selected\n' * 10}), middle)
        last = commit_tree(repo, dict(innocent, **{'end.txt': b'final\n' * 10}), moved)
        app, result, out = self.generate(repo.task(first+','+last, last, multi=True), False)
        self.assert_success(app)
        self.assertEqual([('start.txt', 'end.txt', ChangeType.RENAMED)],
                         [(f.old_path, f.file_path, f.change_type) for f in result.files])
        self.assert_delivered(result, out, {'start.txt': b'original\n' * 10}, {'end.txt': b'final\n' * 10}, False)

    def test_failed_rename_still_blocks_reuse_of_its_supported_side(self):
        repo = Repository(self, 'git', 'reuse')
        content = b'important old identity\n' * 20
        old = commit_tree(repo, {'a.txt': content, 'good.txt': b'old\n'})
        moved = commit_tree(repo, {r'a\b.txt': content, 'good.txt': b'old\n'}, old)
        new = commit_tree(repo, {r'a\b.txt': content, 'a.txt': b'unrelated new file\n', 'good.txt': b'new\n'}, moved)
        app, result, out = self.generate(repo.task(moved+','+new, new, multi=True), False)
        self.assert_success(app)
        self.assert_delivered(result, out, {'good.txt': b'old\n'}, {'good.txt': b'new\n'}, True)
        self.assertTrue(any(i['stage'] == '路径交付依赖' for i in result.issues))

    def test_failed_rename_blocks_directory_replacing_its_supported_old_path(self):
        repo = Repository(self, 'git', 'directory-reuse')
        content = b'important identity\n' * 20
        old = commit_tree(repo, {'a.txt': content, 'good.txt': b'old\n'})
        new = commit_tree(repo, {r'a\b.txt': content, 'a.txt/child.txt': b'child\n', 'good.txt': b'new\n'}, old)
        for multi in (False, True):
            app, result, out = self.generate(repo.task(new if multi else old, new, multi=multi), False, self.root/str(multi))
            self.assert_success(app)
            self.assert_delivered(result, out, {'good.txt': b'old\n'}, {'good.txt': b'new\n'}, True)
            self.assertTrue(any(i['stage'] == '目录替换依赖' for i in result.issues))

    def test_excluded_backslash_name_cannot_block_unrelated_slash_addition(self):
        repo = Repository(self, 'git', 'excluded-literal')
        old = commit_tree(repo, {r'a\b.txt': b'excluded old\n'})
        new = commit_tree(repo, {r'a\b.txt': b'excluded new\n', 'a/b.txt': b'new real path\n'}, old)
        for multi in (False, True):
            task = repo.task(new if multi else old, new, multi=multi)
            task['exclude_rules'] = 'a*b.txt'
            app, result, out = self.generate(task, False, self.root/str(multi))
            self.assert_success(app)
            self.assert_delivered(result, out, {}, {'a/b.txt': b'new real path\n'}, False)

    def test_excluded_unsupported_rename_side_leaves_valid_addition(self):
        repo = Repository(self, 'git', 'excluded-side')
        content = b'kept content\n' * 10
        old = commit_tree(repo, {r'a\b.unsupported': content})
        new = commit_tree(repo, {'kept.txt': content}, old)
        for multi in (False, True):
            task = repo.task(new if multi else old, new, multi=multi)
            task['exclude_rules'] = '*.unsupported'
            app, result, out = self.generate(task, False, self.root/str(multi))
            self.assert_success(app)
            self.assertEqual([ChangeType.ADDED], [f.change_type for f in result.files])
            self.assert_delivered(result, out, {}, {'kept.txt': content}, False)


class EndpointLabelTests(WorkflowCase):
    def assert_contents(self, out, old, new, prefix='Demo'):
        for side, expected in (('oldVersion', old), ('newVersion', new)):
            root = out/side/prefix if prefix else out/side
            self.assertEqual(expected, {p.relative_to(root).as_posix(): p.read_bytes()
                                       for p in root.rglob('*') if p.is_file()})

    def test_git_reserved_labels_do_not_select_the_wrong_snapshot(self):
        repo = Repository(self, 'git', 'labels')
        before, after = {'a.txt': b'old\n', 'app.jar': package(b'old')}, {'a.txt': b'new\n', 'app.jar': package(b'new')}
        repo.commit(before); last = repo.commit(after)
        for index, label in enumerate(('new', 'old', '文件级首尾端点')):
            with self.subTest(label=label):
                repo.cmd('tag', label, last)
                app, result, out = self.generate(repo.task(label, '', multi=True), True, self.root/f'gui-{index}')
                self.assert_success(app)
                self.assertFalse(result.delivery_incomplete, result.issues)
                self.assertEqual(label, result.old_version)
                self.assert_contents(out, before, after)
                self.assertEqual((1, 1), next((f.added_lines, f.deleted_lines) for f in result.files if f.file_path == 'a.txt'))
                archive = next(f for f in result.files if f.file_path == 'app.jar')
                self.assertNotEqual('identical', archive.archive_details['status'])
                vcs = GitMultiVersionVCS(str(repo.path), [label]); self.addCleanup(vcs.cleanup)
                strict = DiffEngine(vcs).generate_diff(label, vcs.new_version_label)
                target = self.root/f'strict-{index}'
                FileExporter(strict, vcs).export(str(target/'oldVersion'), str(target/'newVersion'))
                self.assert_contents(target, before, after, prefix='')

    def test_relative_folder_labels_preserve_rename_and_new_content(self):
        for index, (old_name, new_name) in enumerate((('previous', 'old'), ('new', 'current'))):
            before = {'a.txt': b'before\n', 'source.txt': b'renamed\n', 'app.jar': package(b'before')}
            after = {'a.txt': b'after\n', 'target.txt': b'renamed\n', 'app.jar': package(b'after')}
            for name, contents in ((old_name, before), (new_name, after)):
                (self.root/name).mkdir()
                for path, data in contents.items():
                    (self.root/name/path).write_bytes(data)
            previous_cwd = Path.cwd()
            try:
                os.chdir(self.root)
                task = dict(vcs_type='folder', old_version=old_name, new_version=new_name, project_name='Demo')
                app, result, out = self.generate(task, True, self.root/f'out-{index}')
                self.assert_success(app)
                self.assertFalse(result.delivery_incomplete, result.issues)
                self.assert_contents(out, before, after)
                self.assertEqual((old_name, new_name), (result.old_version, result.new_version))
                self.assertIn(('source.txt', 'target.txt'), {(f.old_path, f.file_path) for f in result.files})
                vcs = FolderVCS(old_name, new_name)
                try:
                    strict = DiffEngine(vcs).generate_diff(old_name, new_name)
                    enrich_archive_reports(strict, vcs)
                    self.assertEqual('after\n', vcs.get_file_content_working('a.txt'))
                    target = self.root/f'strict-{index}'
                    FileExporter(strict, vcs).export(str(target/'oldVersion'), str(target/'newVersion'))
                    self.assert_contents(target, before, after, prefix='')
                finally:
                    vcs.cleanup()
            finally:
                os.chdir(previous_cwd)


class ControlCharacterTests(WorkflowCase):
    def test_soh_survives_all_change_types_context_and_fragment_storage(self):
        original = b'alpha\x01beta\n'
        cases = [(ChangeType.MODIFIED, original, b'alphabeta\n'),
                 (ChangeType.MODIFIED, original, 'alpha⟦U+0001⟧beta\n'.encode()),
                 (ChangeType.RENAMED, original, b'alphabeta\n'),
                 (ChangeType.ADDED, b'', original), (ChangeType.DELETED, original, b'')]
        for index, (kind, old, new) in enumerate(cases):
            for full in (False, True):
                for stored in (False, True):
                    with self.subTest(kind=kind, index=index, full=full, stored=stored), HtmlDetailStore() as store:
                        vcs = BytesVCS(old, new, kind)
                        result = DiffEngine(vcs, show_full_context=full, detail_store=store if stored else None).generate_diff('old', 'new')
                        file = result.files[0]
                        text = ''.join(file.html_fragment.iter_text()) if stored else file.side_by_side_html
                        rows = TableRows(text)
                        expected = lambda data: [(1, data.decode().rstrip('\n').replace('\x01', '⟦U+0001⟧'))] if data else []
                        self.assertEqual(expected(old), rows.side())
                        self.assertEqual(expected(new), rows.side(True))
                        self.assertIn('class="special-separator"', text)
                        self.assertNotIn('\x01', text)
                        self.assertTrue(any(f'class="diff_{style}"' in text for style in ('add', 'sub', 'chg')))
                        self.assertEqual((int(bool(new)), int(bool(old))), (file.added_lines, file.deleted_lines))
                        out = self.root/f'{index}-{full}-{stored}'
                        FileExporter(result, vcs).export(str(out/'old'), str(out/'new'))
                        ReportGenerator().generate(result, str(out/'report.html'))
                        self.assertIn('⟦U+0001⟧', (out/'report.html').read_text('utf-8'))
                        if old:
                            self.assertEqual(old, (out/'old'/('before.txt' if kind == ChangeType.RENAMED else 'file.txt')).read_bytes())
                        if new:
                            self.assertEqual(new, (out/'new/file.txt').read_bytes())


class CacheMarkerRecoveryTests(WorkflowCase):
    def test_bad_marker_shapes_are_preserved_while_valid_stale_cache_is_reclaimed(self):
        root = self.root/'runtime'; root.mkdir()
        old = time.time() - 9*24*3600
        valid = dict(magic=temp_storage._TEMP_MAGIC, created=old, pid=999999)
        invalid = [[], None, 123, 'text', {}, dict(valid, created=None), dict(valid, pid=[]),
                   dict(valid, created=True), dict(valid, pid=True), dict(valid, created=float('nan')),
                   dict(valid, created=float('inf')), dict(valid, created=10**400), dict(valid, pid=0)]
        retained = {}
        for index, payload in enumerate(invalid):
            path = root/f'comparetool_svn_raw_invalid_{index}'; path.mkdir()
            marker = Path(str(path) + temp_storage._TEMP_MARKER_SUFFIX)
            marker.write_text(json.dumps(payload), encoding='utf-8')
            retained[path] = marker.read_bytes()
        stale = root/'comparetool_svn_raw_stale'; stale.mkdir()
        Path(str(stale) + temp_storage._TEMP_MARKER_SUFFIX).write_text(json.dumps(valid), encoding='utf-8')
        with mock.patch.object(temp_storage, '_CLEANED_ROOTS', set()), \
                mock.patch.object(temp_storage, 'pid_is_alive', return_value=False):
            created = temp_storage.create_temp_dir('comparetool_folder_old_')
        self.assertTrue(Path(created).is_dir())
        self.assertFalse(stale.exists())
        for path, content in retained.items():
            self.assertTrue(path.is_dir())
            self.assertEqual(content, Path(str(path) + temp_storage._TEMP_MARKER_SUFFIX).read_bytes())
