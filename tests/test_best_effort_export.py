"""实际工作线程：降级后仍交付可验证文件，并明确列出未完成项。"""
from pathlib import Path
import os
import subprocess
from unittest import mock

from archive_workflow_fixtures import WorkflowCase, Repository, package
from test_archive_report import zip_bytes
from report_generator import ReportGenerator
from file_exporter import FileExporter
from vcs.multi_version_vcs import GitMultiVersionVCS
from vcs.base import ChangeType, EndpointSide
from diff_engine import DiffResult, FileDiff


class BestEffortWorkflowTests(WorkflowCase):
    def commit_index_tree(self, repo, entries, parent=None):
        """直接提交测试索引，覆盖 Windows 工作树无法同时表示的名称和链接。"""
        repo.cmd('read-tree', '--empty')
        for path, (mode, content) in entries.items():
            oid = subprocess.run([repo.executable, 'hash-object', '-w', '--stdin'],
                cwd=repo.path, input=content, capture_output=True, check=True).stdout.decode().strip()
            repo.cmd('update-index', '--add', '--cacheinfo', f'{mode},{oid},{path}')
        args = ['commit-tree', repo.cmd('write-tree'), '-m', 'endpoint fixture']
        if parent:
            args += ['-p', parent]
        version = repo.cmd(*args)
        repo.cmd('update-ref', 'HEAD', version)
        return version

    def test_failed_symlink_rename_blocks_old_directory_replacement(self):
        repo = Repository(self, 'git', 'symlinkReplacement')
        old = self.commit_index_tree(repo, {
            'Node/link': ('120000', b'elsewhere'),
            'Node/ordinary.txt': ('100644', b'ordinary old'),
            'good.txt': ('100644', b'old good'),
        })
        new = self.commit_index_tree(repo, {
            'moved': ('120000', b'elsewhere'),
            'Node': ('100644', b'replacement'),
            'good.txt': ('100644', b'new good'),
        }, old)
        app, result, out = self.generate(repo.task(old, new), False)
        self.assert_report(app, result, out, partial=True)
        self.assertEqual(['good.txt'], [file.file_path for file in result.files])
        self.assertEqual([], result.required_directory_deletions)
        self.assertEqual(b'new good', (out/'newVersion/Demo/good.txt').read_bytes())
        self.assertFalse((out/'newVersion/Demo/Node').exists())
        self.assertFalse((out/'oldVersion/Demo/Node/ordinary.txt').exists())
        instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
        self.assertNotIn('[删除旧目录后再写入同名文件] Demo/Node', instructions)
        self.assertNotIn('[删除文件] Demo/Node/ordinary.txt', instructions)

    def test_failed_rename_collision_blocks_old_directory_replacement(self):
        repo = Repository(self, 'git', 'collisionReplacement')
        old = self.commit_index_tree(repo, {
            'Node/a.txt': ('100644', b'A' * 128),
            'Node/b.txt': ('100644', b'B' * 128),
            'Node/ordinary.txt': ('100644', b'ordinary old'),
            'good.txt': ('100644', b'old good'),
        })
        new = self.commit_index_tree(repo, {
            'X/dup.txt': ('100644', b'A' * 128),
            'x/DUP.txt': ('100644', b'B' * 128),
            'Node': ('100644', b'replacement'),
            'good.txt': ('100644', b'new good'),
        }, old)
        for multi in (False, True):
            with self.subTest(multi=multi):
                task = repo.task(new if multi else old, new, multi=multi)
                app, result, out = self.generate(task, False, self.root/str(multi))
                self.assert_report(app, result, out, partial=True)
                self.assertEqual(['good.txt'], [file.file_path for file in result.files])
                self.assertEqual([], result.required_directory_deletions)
                self.assertEqual(b'new good', (out/'newVersion/Demo/good.txt').read_bytes())
                for side in ('oldVersion', 'newVersion'):
                    self.assertEqual(['good.txt'], [path.relative_to(out/side/'Demo').as_posix()
                        for path in (out/side/'Demo').rglob('*') if path.is_file()])
                instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
                self.assertNotIn('[删除旧目录后再写入同名文件] Demo/Node', instructions)
                self.assertNotIn('[删除文件] Demo/Node/ordinary.txt', instructions)

    def conflict_replacement_versions(self, repo, directory_to_file, *, special=False):
        old_path, new_path = ('Node/child.txt', 'Node') if directory_to_file else ('Node', 'Node/child.txt')
        entries = {
            'old.txt': ('100644', b'baseline old'),
            old_path: ('100644', b'baseline node'),
            'good.txt': ('100644', b'old good'),
        }
        baseline = self.commit_index_tree(repo, entries)
        entries['old.txt'] = ('100644', b'selected old')
        first = self.commit_index_tree(repo, entries, baseline)
        del entries['old.txt']
        del entries[old_path]
        entries[new_path] = ('120000' if special else '100644', b'intermediate replacement')
        middle = self.commit_index_tree(repo, entries, first)
        entries[new_path] = ('120000' if special else '100644', b'selected replacement')
        entries['good.txt'] = ('100644', b'new good')
        last = self.commit_index_tree(repo, entries, middle)
        return first, last, old_path, new_path

    def test_conflict_fallback_preserves_directory_file_replacements(self):
        for directory_to_file in (True, False):
            with self.subTest(directory_to_file=directory_to_file):
                repo = Repository(self, 'git', f'conflictReplacement{directory_to_file}')
                first, last, old_path, new_path = self.conflict_replacement_versions(repo, directory_to_file)
                expected = {'old.txt': ChangeType.DELETED, old_path: ChangeType.DELETED,
                            new_path: ChangeType.ADDED, 'good.txt': ChangeType.MODIFIED}
                vcs = GitMultiVersionVCS(str(repo.path), [first, last])
                try:
                    self.assertEqual(expected, {file.path: file.change_type for file in vcs.get_changed_files()})
                    self.assertEqual(b'baseline node', vcs.get_file_content_raw_bytes('old', old_path))
                    self.assertEqual(b'selected replacement', vcs.get_file_content_raw_bytes('new', new_path))
                finally:
                    vcs.cleanup()
                task = repo.task(first + '\n' + last, last, multi=True)
                app, result, out = self.generate(task, False, self.root/f'output{directory_to_file}')
                self.assert_success(app)
                self.assertFalse(result.delivery_incomplete)
                self.assertEqual([], result.issues)
                self.assertIn('降级', result.comparison_note)
                self.assertEqual(expected, {file.file_path: file.change_type for file in result.files})
                self.assertEqual(b'baseline node', (out/'oldVersion/Demo'/old_path).read_bytes())
                self.assertEqual(b'selected replacement', (out/'newVersion/Demo'/new_path).read_bytes())
                self.assertEqual(b'new good', (out/'newVersion/Demo/good.txt').read_bytes())
                self.assertEqual(['Node'] if directory_to_file else [], result.required_directory_deletions)
                instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig')
                self.assertIn('[删除文件] Demo/' + old_path, instructions)
                if directory_to_file:
                    self.assertIn('[删除旧目录后再写入同名文件] Demo/Node', instructions)

    def test_conflict_fallback_still_rejects_symlink_endpoint(self):
        repo = Repository(self, 'git', 'conflictSpecialReplacement')
        first, last, _old_path, new_path = self.conflict_replacement_versions(repo, True, special=True)
        with self.assertRaisesRegex(RuntimeError, '不是普通文件'):
            GitMultiVersionVCS(str(repo.path), [first, last])
        task = repo.task(first + '\n' + last, last, multi=True)
        app, result, out = self.generate(task, False)
        self.assert_report(app, result, out, partial=True)
        self.assertTrue(any('不是普通文件' in issue['message'] for issue in result.issues))
        self.assertFalse((out/'newVersion/Demo'/new_path).exists())
        self.assertEqual(b'new good', (out/'newVersion/Demo/good.txt').read_bytes())

    def replacement_task(self):
        old, new = self.root/'replaceOld', self.root/'replaceNew'
        (old/'Node').mkdir(parents=True); new.mkdir()
        (old/'Node/child.txt').write_bytes(b'old child')
        (new/'node').write_bytes(b'new replacement')
        (new/'good.txt').write_bytes(b'independent')
        return dict(vcs_type='folder', project_name='Replace', project_path='',
                    old_version=str(old), new_version=str(new), exclude_rules='')

    def test_case_only_directory_replacement_keeps_old_spelling_instruction(self):
        app, result, out = self.generate(self.replacement_task(), False)
        self.assert_success(app)
        self.assertEqual(['Node'], result.required_directory_deletions)
        self.assertIn('Replace/Node', next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig'))
        self.assertEqual(b'new replacement', (out/'newVersion/Replace/node').read_bytes())

    def test_directory_replacement_failure_blocks_entire_dependent_group(self):
        original = FileExporter._write_file
        for failure in ('Node/child.txt', 'node'):
            with self.subTest(failure=failure):
                task = self.replacement_task() if not (self.root/'replaceOld').exists() else dict(
                    vcs_type='folder', project_name='Replace', project_path='', exclude_rules='',
                    old_version=str(self.root/'replaceOld'), new_version=str(self.root/'replaceNew'))
                def fail(exporter, base, path, version, content):
                    if path == failure:
                        raise OSError('replacement member unavailable')
                    return original(exporter, base, path, version, content)
                with mock.patch.object(FileExporter, '_write_file', fail):
                    app, result, out = self.generate(task, False, self.root/failure.replace('/', '_'))
                self.assert_report(app, result, out, partial=True)
                self.assertEqual(['good.txt'], [file.file_path for file in result.files])
                self.assertEqual([], result.required_directory_deletions)
                self.assertFalse((out/'newVersion/Replace/node').exists())
                self.assertFalse((out/'oldVersion/Replace/Node/child.txt').exists())

    def test_preexisting_failed_old_child_blocks_directory_replacement(self):
        task = self.replacement_task()
        original = FileExporter.prepare_export
        def previous_failure(exporter, *args, **kwargs):
            exporter.diff_result.files = [file for file in exporter.diff_result.files
                                           if file.file_path != 'Node/child.txt']
            exporter.diff_result.add_issue('文件端点', 'Node/child.txt', 'unavailable', delivery=True)
            return original(exporter, *args, **kwargs)
        with mock.patch.object(FileExporter, 'prepare_export', previous_failure):
            app, result, out = self.generate(task, False)
        self.assert_report(app, result, out, partial=True)
        self.assertEqual(['good.txt'], [file.file_path for file in result.files])
        self.assertEqual([], result.required_directory_deletions)

    def test_failure_cannot_publish_changed_successful_endpoint_even_with_restored_metadata(self):
        task = self.folder_task()
        original = FileExporter._write_file
        def tamper(exporter, base, path, version, content):
            if path == 'good.txt':
                previous = Path(base)/'bad.jar'
                info = previous.stat()
                previous.write_bytes(b'X' * info.st_size)
                os.utime(previous, ns=(info.st_atime_ns, info.st_mtime_ns))
                raise OSError('failure after changing previous endpoint')
            return original(exporter, base, path, version, content)
        with mock.patch.object(FileExporter, '_write_file', tamper):
            app, _result, out = self.generate(task, False)
        self.assertFalse(app._last_task_record['success'])
        self.assertFalse((out/'report.html').exists())
        self.assert_no_stages(out)

    def test_actual_windows_short_alias_collision_excludes_both_sides(self):
        if os.name != 'nt':
            self.skipTest('需要 Windows 实际短名')
        import ctypes
        for directory in (False, True):
            with self.subTest(directory=directory):
                probe = self.root/('directoryProbe' if directory else 'fileProbe')
                probe.mkdir()
                long = probe/('longfoldername' if directory else 'longfilename.txt')
                long.mkdir() if directory else long.write_bytes(b'probe')
                buffer = ctypes.create_unicode_buffer(32768)
                length = ctypes.windll.kernel32.GetShortPathNameW(str(long), buffer, len(buffer))
                short_name = Path(buffer.value).name
                if not length or short_name.lower() == long.name.lower():
                    self.skipTest('测试盘未启用 8.3 短名')
                names = ([long.name+'/first.txt', short_name+'/second.txt'] if directory
                         else [long.name, short_name]) + ['good.txt']
                class Writer:
                    def export_file_to_path(self, version, path, target):
                        Path(target).write_bytes(path.encode())
                result = DiffResult('', 'Alias', 'stub', 'old', 'new', files=[
                    FileDiff(name, ChangeType.ADDED) for name in names])
                output = self.root/('directoryOutput' if directory else 'fileOutput')
                output.mkdir()
                FileExporter(result, Writer(), best_effort=True).export(
                    str(output/'oldVersion'), str(output/'newVersion'), 'Alias')
                self.assertEqual(['good.txt'], [file.file_path for file in result.files])
                self.assertEqual(['good.txt'], [path.name for path in (output/'newVersion/Alias').rglob('*') if path.is_file()])
                self.assertEqual(2, len(result.issues))

    def test_multi_version_snapshot_alias_conflict_excludes_both_entities(self):
        if os.name != 'nt':
            self.skipTest('需要 Windows 实际短名')
        for directory in (False, True):
            for deletion in (False, True):
                with self.subTest(directory=directory, deletion=deletion):
                    probe = self.root/f'probe{directory}{deletion}'
                    probe.mkdir()
                    long_name = 'longdirname' if directory else 'longfilename.txt'
                    long = probe/long_name
                    long.mkdir() if directory else long.write_bytes(b'probe')
                    short_name = 'longdi~1' if directory else 'longfi~1.txt'
                    if not (probe/short_name).exists():
                        self.skipTest('测试盘未启用预期 8.3 短名')
                    repo = Repository(self, 'git', f'aliasRepo{directory}{deletion}')
                    base = repo.commit({'unchanged.txt': b'keep'})
                    names = ([long_name+'/first.txt', short_name+'/second.txt'] if directory
                             else [long_name, short_name]) + ['good.txt']
                    for name in names:
                        oid = subprocess.run([repo.executable, 'hash-object', '-w', '--stdin'],
                            cwd=repo.path, input=name.encode(), capture_output=True, check=True).stdout.decode().strip()
                        repo.cmd('update-index', '--add', '--cacheinfo', '100644,'+oid+','+name)
                    tree = repo.cmd('write-tree')
                    selected = repo.cmd('commit-tree', tree, '-p', base, '-m', 'alias fixture')
                    repo.cmd('update-ref', 'HEAD', selected)
                    if deletion:
                        for name in names:
                            repo.cmd('update-index', '--force-remove', '--', name)
                        selected = repo.cmd('commit-tree', repo.cmd('write-tree'), '-p', selected, '-m', 'delete fixture')
                        repo.cmd('update-ref', 'HEAD', selected)
                    task = repo.task(selected, selected, multi=True)
                    app, result, out = self.generate(task, False, self.root/f'aliasOutput{directory}{deletion}')
                    self.assert_report(app, result, out, partial=True)
                    self.assertEqual(['good.txt'], [file.file_path for file in result.files])
                    side = 'oldVersion' if deletion else 'newVersion'
                    self.assertEqual([b'good.txt'], [path.read_bytes() for path in (out/side/'Demo').rglob('*') if path.is_file()])

    def folder_task(self, name='Folder'):
        old, new = self.root/(name+'Old'), self.root/(name+'New')
        old.mkdir(); new.mkdir()
        (old/'bad.jar').write_bytes(package(b'old'))
        (new/'bad.jar').write_bytes(b'broken inner jar')
        (old/'good.txt').write_bytes(b'old\n')
        (new/'good.txt').write_bytes(b'new\n')
        return dict(vcs_type='folder', project_name=name, project_path='',
                    old_version=str(old), new_version=str(new), exclude_rules='',
                    show_full_context=True, show_project_root=True)

    def assert_report(self, app, result, out, *, partial=False):
        self.assert_success(app)
        app._show_error.assert_not_called()
        self.assertTrue((out/'report.html').is_file())
        self.assertEqual(partial, result.delivery_incomplete)
        self.assertEqual('partial' if partial else 'completed_with_notes', app._last_task_record['status'])
        self.assert_no_stages(out)

    def test_corrupt_inner_archive_all_six_modes_keeps_original_delivery(self):
        folder = self.folder_task()
        old_zip, new_zip = self.root/'old.zip', self.root/'new.zip'
        old_zip.write_bytes(zip_bytes({'bad.jar': package(b'old')}))
        new_zip.write_bytes(zip_bytes({'bad.jar': b'broken inner jar'}))
        tasks = [folder, dict(folder, vcs_type='archive', project_name='Archive',
                             old_version=str(old_zip), new_version=str(new_zip))]
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind+'-repository')
            first = repo.commit({'bad.jar': package(b'old')})
            last = repo.commit({'bad.jar': b'broken inner jar'})
            tasks.extend([repo.task(first, last, project_name=kind),
                          repo.task(last, last, multi=True, project_name=kind+'Multi')])
        for index, task in enumerate(tasks):
            with self.subTest(mode=task['vcs_type']):
                app, result, out = self.generate(task, True, self.root/str(index))
                self.assert_report(app, result, out)
                self.assertEqual(b'broken inner jar', (out/'newVersion'/task['project_name']/'bad.jar').read_bytes())
                self.assertEqual('error', next(file for file in result.files if file.file_path=='bad.jar').archive_details['status'])
                self.assertIn('包内分析未完成', (out/'report.html').read_text(encoding='utf-8'))

    def test_failed_file_has_no_half_export_or_delete_instruction(self):
        task = self.folder_task()
        original = FileExporter._write_file
        # Match the explicit snapshot side, independently of display labels and stage names.
        def fail_new(exporter, base, path, version, content):
            if path == 'bad.jar' and version is EndpointSide.NEW:
                raise OSError('injected new endpoint failure')
            return original(exporter, base, path, version, content)
        with mock.patch.object(FileExporter, '_write_file', fail_new):
            app, result, out = self.generate(task, False)
        self.assert_report(app, result, out, partial=True)
        self.assertTrue(any('injected new endpoint failure' in issue['message'] for issue in result.issues))
        self.assertEqual(['good.txt'], [file.file_path for file in result.files])
        for side in ('oldVersion', 'newVersion'):
            self.assertFalse((out/side/'Folder'/'bad.jar').exists())
        self.assertEqual(b'new\n', (out/'newVersion'/'Folder'/'good.txt').read_bytes())
        self.assertIn('交付未完成', next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig'))

    def test_unsupported_conversion_keeps_independent_files_in_git_and_svn_modes(self):
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind+'-conversion')
            first = repo.commit({'bad.txt': b'old bad\n', 'good.txt': b'old good\n'})
            if kind == 'git':
                repo.cmd('config', 'filter.check.smudge', 'echo must-not-run')
                changes = {'.gitattributes': b'bad.txt filter=check\n'}
            else:
                repo.cmd('propset', 'svn:keywords', 'Id', 'bad.txt')
                changes = {}
            last = repo.commit(dict(changes, **{'bad.txt': b'new bad\n', 'good.txt': b'new good\n'}))
            for multi in (False, True):
                with self.subTest(kind=kind, multi=multi):
                    task = repo.task(last if multi else first, last, multi=multi)
                    app, result, out = self.generate(task, False, self.root/(kind+str(multi)))
                    self.assert_report(app, result, out, partial=True)
                    self.assertEqual(b'new good\n', (out/'newVersion/Demo/good.txt').read_bytes())
                    self.assertFalse((out/'newVersion/Demo/bad.txt').exists())
                    self.assertFalse((out/'oldVersion/Demo/bad.txt').exists())

    def test_failed_writer_cannot_publish_unlisted_stage_file(self):
        task = self.folder_task()
        original = FileExporter._write_file
        def leave_unverified(exporter, base, path, version, content):
            if path == 'bad.jar':
                (Path(base)/'unverified.txt').write_bytes(b'unverified')
                raise OSError('injected writer interruption')
            return original(exporter, base, path, version, content)
        with mock.patch.object(FileExporter, '_write_file', leave_unverified):
            app, result, out = self.generate(task, False)
        self.assertFalse(app._last_task_record['success'])
        self.assertFalse((out/'report.html').exists())
        self.assertFalse(list(out.rglob('unverified.txt')))
        self.assert_no_stages(out)

    def test_unreadable_partial_stage_subtree_cannot_hide_unlisted_files(self):
        task = self.folder_task()
        original = FileExporter._write_file
        scandir = os.scandir
        rejected = []
        def leave_unverified(exporter, base, path, version, content):
            if path == 'bad.jar':
                extra = Path(base)/'unreadable'
                extra.mkdir(exist_ok=True)
                (extra/'unverified.txt').write_bytes(b'unverified')
                raise OSError('injected writer interruption')
            return original(exporter, base, path, version, content)
        def unreadable(path):
            if isinstance(path, (str, bytes, os.PathLike)) and Path(path).name == 'unreadable' and not rejected:
                rejected.append(str(path))
                raise PermissionError('injected inaccessible subtree')
            return scandir(path)
        with mock.patch.object(FileExporter, '_write_file', leave_unverified), mock.patch('os.scandir', unreadable):
            app, _result, out = self.generate(task, False)
        self.assertTrue(rejected)
        self.assertFalse(app._last_task_record['success'])
        self.assertFalse((out/'report.html').exists())
        self.assertFalse(list(out.rglob('unverified.txt')))
        self.assert_no_stages(out)

    def test_multi_failed_project_and_display_collision_still_export(self):
        folder = self.folder_task()
        failed = dict(folder, project_name='Failed', old_version=str(self.root/'missing'))
        tasks = [dict(folder, project_name=name, recursive_archives=True, show_project_root=False)
                 for name in ('Alpha', 'Beta')] + [failed]
        out = self.root/'multi'; out.mkdir()
        captured = []
        original = ReportGenerator.generate_multi
        def observe(generator, results, *args, **kwargs):
            captured.extend(results)
            return original(generator, results, *args, **kwargs)
        app = self.app()
        with mock.patch.object(ReportGenerator, 'generate_multi', observe):
            app._do_generate_multi(tasks, str(out/'report.html'), str(out/'oldVersion'),
                                   str(out/'newVersion'), str(out))
        self.assert_success(app)
        app._show_error.assert_not_called()
        self.assertEqual('partial', app._last_task_record['status'])
        for item in captured[:2]:
            self.assertTrue(item['show_project_root'])
            self.assertFalse(item['diff_result'].delivery_incomplete)
            self.assertEqual(b'new\n', (out/'newVersion'/item['project_name']/'good.txt').read_bytes())
        self.assertFalse(tasks[0]['show_project_root'])
        self.assertTrue(captured[2]['diff_result'].delivery_incomplete)
        self.assertIn('missing', (out/'report.html').read_text(encoding='utf-8'))
        self.assert_no_stages(out)

    def test_template_failure_outputs_basic_report_and_full_files(self):
        task = self.folder_task()
        with mock.patch('jinja2.Environment.get_template', side_effect=RuntimeError('broken template')):
            app, result, out = self.generate(task, False)
        self.assert_report(app, result, out)
        self.assertIn('基础比对报告', (out/'report.html').read_text(encoding='utf-8'))
        self.assertEqual(b'new\n', (out/'newVersion'/'Folder'/'good.txt').read_bytes())

    def test_unrepresentable_error_text_does_not_block_report_or_instructions(self):
        task = self.folder_task()
        original = FileExporter._write_file
        def fail_bad(exporter, base, path, version, content):
            if path == 'bad.jar':
                raise OSError('invalid repository filename: \udcff')
            return original(exporter, base, path, version, content)
        with mock.patch.object(FileExporter, '_write_file', fail_bad):
            app, result, out = self.generate(task, False)
        self.assert_report(app, result, out, partial=True)
        self.assertIn('\\udcff', (out/'report.html').read_text(encoding='utf-8'))
        self.assertIn('\\udcff', next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig'))
        self.assertEqual(b'new\n', (out/'newVersion/Folder/good.txt').read_bytes())

    def test_single_unreadable_project_outputs_explicit_diagnostic_report(self):
        task = self.folder_task()
        task['old_version'] = str(self.root/'missing')
        app, result, out = self.generate(task, False)
        self.assert_report(app, result, out, partial=True)
        self.assertEqual([], result.files)
        self.assertIn('交付未完成', (out/'report.html').read_text(encoding='utf-8'))

    def test_archive_root_mismatch_falls_back_both_roots(self):
        old, new = self.root/'old.zip', self.root/'new.zip'
        old.write_bytes(zip_bytes({'wrapper/one.txt': b'old'}))
        new.write_bytes(zip_bytes({'one.txt': b'new', 'two.txt': b'two'}))
        task = dict(vcs_type='archive', project_name='Roots', project_path='',
                    old_version=str(old), new_version=str(new), ignore_archive_root=True)
        app, result, out = self.generate(task, False)
        self.assert_success(app)
        self.assertIn('未生效', result.comparison_note)
        self.assertEqual(b'old', (out/'oldVersion'/'Roots'/'wrapper'/'one.txt').read_bytes())
        self.assertEqual(b'new', (out/'newVersion'/'Roots'/'one.txt').read_bytes())

    def test_noncontiguous_conflict_keeps_unrelated_file_own_endpoint(self):
        repo = Repository(self, 'git', 'conflict')
        base = repo.commit({'old.jar': b'old baseline', 'own.txt': b'own baseline'})
        first = repo.commit({'old.jar': b'old selected', 'own.txt': b'own selected'})
        (repo.path/'old.jar').unlink()
        repo.commit({'new.jar': b'completely different\x00', 'own.txt': b'unselected later'})
        last = repo.commit({'new.jar': b'completely different\x00\x01'})
        vcs = GitMultiVersionVCS(str(repo.path), [first, last])
        try:
            changes = {file.path:file.change_type for file in vcs.get_changed_files()}
            self.assertEqual({'old.jar':ChangeType.DELETED, 'new.jar':ChangeType.ADDED,
                              'own.txt':ChangeType.MODIFIED}, changes)
            self.assertEqual(b'own selected', vcs.get_file_content_raw_bytes('new', 'own.txt'))
            self.assertFalse((Path(vcs._new_dir)/'old.jar').exists())
            self.assertEqual(b'completely different\x00\x01', vcs.get_file_content_raw_bytes('new','new.jar'))
        finally:
            vcs.cleanup()

    def test_conflict_group_includes_unselected_successor_paths(self):
        repo = Repository(self, 'git', 'successors')
        repo.commit({'old.txt': b'old baseline'})
        first = repo.commit({'old.txt': b'old selected', 'middle.txt': b'middle selected'})
        (repo.path/'old.txt').unlink(); (repo.path/'middle.txt').unlink()
        repo.commit({'new.txt': b'new content completely rewritten',
                     'final.txt': b'final content completely rewritten'})
        last = repo.commit({'new.txt': b'new selected'})
        vcs = GitMultiVersionVCS(str(repo.path), [first,last])
        try:
            changes = {file.path:file.change_type for file in vcs.get_changed_files()}
            self.assertEqual({'old.txt':ChangeType.DELETED, 'new.txt':ChangeType.ADDED,
                              'final.txt':ChangeType.ADDED}, changes)
            self.assertFalse((Path(vcs._new_dir)/'middle.txt').exists())
            self.assertEqual(b'final content completely rewritten',
                             vcs.get_file_content_raw_bytes('new','final.txt'))
        finally:
            vcs.cleanup()
