"""真实仓库输出隔离、深层包内明细及失败重命名诊断的交付回归。"""
import hashlib
from html.parser import HTMLParser
import re
import subprocess
import sys
from unittest import mock

from archive_workflow_fixtures import Repository, WorkflowCase
import archive_report
from archive_report import ArchiveReportBudget, enrich_archive_reports
from diff_engine import DiffEngine, DiffResult, FileDiff
from file_exporter import FileExporter
from main import CompareToolApp
from report_generator import ReportGenerator
from test_archive_report import zip_bytes
from vcs.base import ChangeType
from vcs.folder_vcs import FolderVCS


class ExternalGitMetadataTests(WorkflowCase):
    def repository(self):
        repo = Repository(self, 'git', 'external')
        old = repo.commit({'a.txt': b'old\n'})
        new = repo.commit({'a.txt': b'new\n'})
        metadata = repo.base / 'metadata'
        repo.cmd('init', '--quiet', '--separate-git-dir=' + str(metadata))
        repo.cmd('branch', 'Demo_diff.html')
        linked = repo.base / 'linked'
        repo.cmd('worktree', 'add', '--detach', str(linked), new)
        private = repo.cmd('rev-parse', '--absolute-git-dir', cwd=linked)
        return repo, old, new, metadata, linked, private

    def test_separate_and_linked_metadata_never_reach_output_deletion(self):
        repo, old, new, metadata, linked, private = self.repository()
        reference = metadata / 'refs/heads/Demo_diff.html'
        before = reference.read_bytes()
        for checkout in (repo.path, linked):
            for mode in ('git', 'git_multi'):
                with self.subTest(checkout=checkout.name, mode=mode):
                    for target in (reference, metadata / 'objects/output', private,
                                   repo.base):
                        with self.assertRaisesRegex(ValueError, '元数据'):
                            CompareToolApp._validate_repository_output_targets(
                                str(checkout), mode, [str(target)])
                    task = repo.task(new if mode == 'git_multi' else old, new,
                                     multi=mode == 'git_multi', scope=checkout)
                    out = reference.parent
                    for multi_report in (False, True):
                        app = self.app()
                        with mock.patch.object(FileExporter, '_clear_outputs',
                                side_effect=AssertionError('must not delete')) as clear:
                            if multi_report:
                                app._do_generate_multi([task], str(reference),
                                    str(out / 'oldVersion'), str(out / 'newVersion'), str(out))
                            else:
                                app._do_generate(str(checkout), mode, task['old_version'],
                                    task['new_version'], 'Demo', [], True, True,
                                    str(reference), str(out / 'oldVersion'),
                                    str(out / 'newVersion'), str(out))
                        clear.assert_not_called()
                        self.assertEqual('failed', app._last_task_record['status'])
                        self.assertIn('元数据', app._show_error.call_args.args[0])
        self.assertEqual(before, reference.read_bytes())
        self.assertEqual(new, repo.cmd('rev-parse', 'refs/heads/Demo_diff.html'))

    def test_external_and_untracked_worktree_outputs_remain_available(self):
        repo, old, new, metadata, linked, _private = self.repository()
        for index, checkout in enumerate((repo.path, linked)):
            for multi in (False, True):
                task = repo.task(new if multi else old, new, multi=multi, scope=checkout)
                out = (checkout / 'untracked-output' if multi else
                       repo.base / 'metadata-output') / str(index)
                app, result, output = self.generate(task, False, out)
                self.assert_success(app)
                self.assertFalse(result.issues)
                self.assertEqual(b'old\n', (output / 'oldVersion/Demo/a.txt').read_bytes())
                self.assertEqual(b'new\n', (output / 'newVersion/Demo/a.txt').read_bytes())
                self.assert_no_stages(output)

    def test_unreadable_metadata_identity_stops_before_clear(self):
        repo, old, new, _metadata, _linked, _private = self.repository()
        app = self.app()
        out = self.root / 'safe-output'
        original = subprocess.run

        def fail_metadata(command, *args, **kwargs):
            if '--git-common-dir' in command:
                return subprocess.CompletedProcess(command, 128, b'', b'metadata unavailable')
            return original(command, *args, **kwargs)

        with mock.patch('main.subprocess.run', side_effect=fail_metadata), \
                mock.patch.object(FileExporter, '_clear_outputs') as clear:
            app._do_generate(str(repo.path), 'git', old, new, 'Demo', [], True, True,
                str(out / 'report.html'), str(out / 'oldVersion'), str(out / 'newVersion'), str(out))
        clear.assert_not_called()
        self.assertEqual('failed', app._last_task_record['status'])
        self.assertIn('元数据', app._show_error.call_args.args[0])


class DeepArchiveTraversalTests(WorkflowCase):
    @staticmethod
    def nested(body, depth):
        data = zip_bytes({'last.txt': body})
        for _ in range(depth - 1):
            data = zip_bytes({'inner.jar': data})
        return data

    def test_400_real_layers_reach_report_without_changing_delivery(self):
        before, after = self.root / 'old', self.root / 'new'
        before.mkdir(); after.mkdir()
        old = self.nested(b'OLD_DEEPEST_MARKER', 400)
        new = self.nested(b'NEW_DEEPEST_MARKER', 400)
        (before / 'app.zip').write_bytes(old)
        (after / 'app.zip').write_bytes(new)
        recursion_limit = sys.getrecursionlimit()
        task = dict(vcs_type='folder', project_path='', project_name='Demo',
                    old_version=str(before), new_version=str(after), exclude_rules='')
        app, result, out = self.generate(task, True)
        self.assert_success(app)
        self.assertEqual('completed', app._last_task_record['status'], result.issues)
        self.assertFalse(result.issues)
        item, depth = result.files[0], 0
        while item.archive_details:
            detail = item.archive_details
            self.assertEqual('compared', detail['status'])
            self.assertEqual(1, len(detail['members']))
            item = detail['members'][0]
            depth += 1
        self.assertEqual((400, 'last.txt'), (depth, item.file_path))
        visible = []

        class Text(HTMLParser):
            def handle_data(self, data):
                visible.append(data)

        Text().feed((out / 'report.html').read_text('utf-8'))
        self.assertIn('NEW_DEEPEST_MARKER', ''.join(visible))
        self.assertEqual(1, result.summary['total_files'])
        for side, payload in (('oldVersion', old), ('newVersion', new)):
            self.assertEqual(hashlib.sha256(payload).digest(), hashlib.sha256(
                (out / side / 'Demo/app.zip').read_bytes()).digest())
        self.assertEqual(recursion_limit, sys.getrecursionlimit())
        self.assert_no_stages(out)
        self.assertFalse(list((self.root / 'runtime').iterdir()))

    def test_2000_layer_single_and_multi_html_preserve_all_members_and_escaping(self):
        leaf = FileDiff('last<&>.txt', ChangeType.MODIFIED,
                       side_by_side_html='<p>DEEPEST_RENDER_MARKER</p>')
        for _ in range(2000):
            inner = DiffResult('', 'Demo', 'archive', 'old', 'new', files=[leaf])
            leaf = FileDiff('inner<&>.jar', ChangeType.MODIFIED, archive_details=dict(
                status='compared', members=[leaf], counts=inner.summary, filtered=False))
        result = DiffResult('', 'Demo', 'folder', 'old', 'new', files=[leaf],
                            archive_details_enabled=True)
        for multi in (False, True):
            report = self.root / ('multi.html' if multi else 'single.html')
            generator = ReportGenerator(best_effort=True)
            if multi:
                generator.generate_multi([dict(project_name='Demo', show_project_root=True,
                    vcs_type='folder', diff_result=result)], str(report))
            else:
                generator.generate(result, str(report))
            text = report.read_text('utf-8')
            self.assertFalse(result.issues)
            self.assertIn('DEEPEST_RENDER_MARKER', text)
            self.assertIn('last&lt;&amp;&gt;.txt', text)
            self.assertEqual(2000, text.count('<template class="archive-member-template">'))

    def test_deep_parse_errors_and_explicit_depth_policy_clean_every_owned_directory(self):
        for best_effort in (False, True):
            for limited in (False, True):
                with self.subTest(best_effort=best_effort, limited=limited):
                    old, new = self.root / f'old-{best_effort}-{limited}', self.root / f'new-{best_effort}-{limited}'
                    old.mkdir(); new.mkdir()
                    data = self.nested(b'leaf', 7) if limited else b'broken inner archive'
                    if not limited:
                        for _ in range(7):
                            data = zip_bytes({'inner.jar': data})
                    (new / 'app.zip').write_bytes(data)
                    (new / 'good.zip').write_bytes(zip_bytes({'good.txt': b'INDEPENDENT_MARKER'}))
                    vcs = FolderVCS(str(old), str(new))
                    try:
                        result = DiffEngine(vcs).generate_diff('old', 'new')
                        budget = ArchiveReportBudget(max_depth=3 if limited else None)
                        if best_effort:
                            enrich_archive_reports(result, vcs, budget=budget, best_effort=True)
                            self.assertEqual(1, len(result.issues))
                            self.assertFalse(result.delivery_incomplete)
                            report = self.root / f'errors-{limited}.html'
                            ReportGenerator().generate(result, str(report))
                            self.assertIn('INDEPENDENT_MARKER', report.read_text('utf-8'))
                        else:
                            with self.assertRaises(RuntimeError):
                                enrich_archive_reports(result, vcs, budget=budget)
                    finally:
                        vcs.cleanup()
                    self.assertFalse(list((self.root / 'runtime').iterdir()))

    def test_interruption_closes_suspended_parent_archives_and_empty_sides(self):
        old, new = self.root / 'old', self.root / 'new'
        old.mkdir(); new.mkdir()
        (new / 'app.zip').write_bytes(self.nested(b'leaf', 7))
        vcs = FolderVCS(str(old), str(new))
        original = archive_report._Inspector.inspect
        opened = 0

        def interrupt(inspector, *args):
            nonlocal opened
            opened += 1
            if opened == 5:
                raise KeyboardInterrupt('test interruption')
            return original(inspector, *args)

        try:
            result = DiffEngine(vcs).generate_diff('old', 'new')
            with mock.patch.object(archive_report._Inspector, 'inspect', interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    enrich_archive_reports(result, vcs)
        finally:
            vcs.cleanup()
        self.assertEqual(5, opened)
        self.assertFalse(list((self.root / 'runtime').iterdir()))


class FailedRenameDiagnosticsTests(WorkflowCase):
    def test_failed_rename_keeps_both_paths_in_visible_report_and_instructions(self):
        repo = Repository(self, 'git', 'rename')
        old = repo.commit({'before&name.txt': b'unchanged\n', 'good.py': b'old\n'})
        repo.cmd('mv', 'before&name.txt', 'after.txt')
        new = repo.commit({'.gitattributes': b'after.txt filter=Blocked\n', 'good.py': b'new\n'})
        repo.cmd('config', 'filter.Blocked.required', 'true')
        for multi in (False, True):
            task = repo.task(old, new)
            out = self.root / ('multi' if multi else 'single')
            if multi:
                out.mkdir()
                app = self.app()
                app._do_generate_multi([task], str(out / 'report.html'),
                    str(out / 'oldVersion'), str(out / 'newVersion'), str(out))
            else:
                app, _result, out = self.generate(task, False, out)
            self.assert_success(app)
            self.assertEqual('partial', app._last_task_record['status'])
            text = (out / 'report.html').read_text('utf-8')
            visible = ''.join(re.findall(r'<aside\b.*?</aside>', text, flags=re.S))
            self.assertIn('before&amp;name.txt', visible)
            self.assertIn('after.txt', visible)
            instructions = next(out.glob('*操作说明.txt')).read_text('utf-8-sig')
            self.assertIn('before&name.txt', instructions)
            self.assertIn('after.txt', instructions)
            self.assertNotIn('[重命名旧路径] Demo/before&name.txt', instructions)
            self.assertFalse((out / 'oldVersion/Demo/before&name.txt').exists())
            self.assertFalse((out / 'newVersion/Demo/after.txt').exists())
            self.assertEqual(b'new\n', (out / 'newVersion/Demo/good.py').read_bytes())
            self.assert_no_stages(out)

    def test_basic_report_preserves_old_path_as_escaped_diagnostic_only(self):
        result = DiffResult('', 'Demo', 'Git', 'old', 'new')
        result.add_issue('文件导出', 'after.txt', 'failed', delivery=True,
                         old_path='<before&name>.txt')
        report = self.root / 'fallback.html'
        generator = ReportGenerator(best_effort=True)
        with mock.patch.object(generator.env, 'get_template', side_effect=RuntimeError('template failed')):
            generator.generate(result, str(report))
        text = report.read_text('utf-8')
        self.assertIn('&lt;before&amp;name&gt;.txt', text)
        self.assertNotIn('<before&name>', text)
        self.assertEqual([], result.files)
