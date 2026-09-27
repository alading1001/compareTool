"""Stage-side identity, transaction binding and failure contracts (plan F/B)."""
import os
from pathlib import Path
import subprocess
from unittest import mock

from archive_endpoints import StagedArchiveEndpoints
from archive_report import enrich_archive_reports, _is_lfs_pointer
from diff_engine import DiffResult, FileDiff
from file_exporter import FileExporter
from vcs.base import ChangeType
from archive_workflow_fixtures import WorkflowCase, package, tree_hashes


class ArchiveStagingTests(WorkflowCase):
    def staged(self):
        out = self.root/'stage-test'; out.mkdir()
        old, new = out/'old-stage', out/'new-stage'
        old.mkdir(); new.mkdir()
        (old/'a.jar').write_bytes(package(b'OLD_VALUE'))
        (new/'a.jar').write_bytes(package(b'NEW_VALUE'))
        return out, old, new, StagedArchiveEndpoints(old, new, trusted_root=out)

    def result(self, change=ChangeType.MODIFIED, name='a.jar', old_path=''):
        return DiffResult('source', 'Demo', 'GitMultiVersionVCS', 'abc, def',
            '文件级首尾端点', files=[FileDiff(name, change, old_path=old_path)])

    def test_explicit_sides_ignore_display_labels_and_never_call_vcs(self):
        out, old, new, endpoints = self.staged()
        before = tree_hashes(out)
        class NoVCS:
            def __getattribute__(self, name):
                raise AssertionError('Unexpected repository read: ' + name)
        result = self.result()
        enrich_archive_reports(result, NoVCS(), endpoints=endpoints)
        text = result.files[0].archive_details['members'][0].side_by_side_html
        self.assertIn('OLD', text); self.assertIn('NEW', text)
        self.assertEqual('abc, def', result.old_version)
        self.assertEqual('文件级首尾端点', result.new_version)
        self.assertEqual(before, tree_hashes(out))
        for invalid in ('abc', '文件级首尾端点', '', None, False):
            with self.assertRaises(ValueError): endpoints.path_for(invalid, 'a.jar')
        self.assertEqual(str(old/'a.jar'), endpoints.path_for('old', 'a.jar'))
        self.assertEqual(str(new/'a.jar'), endpoints.path_for('new', 'a.jar'))

    def test_export_pairs_resolve_by_targets_and_reject_ambiguous_pairs(self):
        out, old, new, _ = self.staged()
        targets = [out/'deliver-old', out/'deliver-new']
        e = StagedArchiveEndpoints.from_export_pairs(
            [(new, targets[1]), (old, targets[0])], *targets, trusted_root=out)
        self.assertEqual(str(old/'a.jar'), e.path_for('old', 'a.jar'))
        for pairs in ([(old, targets[0])], [(old, targets[0]), (new, targets[0])],
                      [(old, targets[0]), (new, out/'wrong')]):
            with self.assertRaises(ValueError):
                StagedArchiveEndpoints.from_export_pairs(pairs, *targets, trusted_root=out)
        for name in ('../a.jar', 'a.jar:ads', 'missing.jar'):
            with self.assertRaises(RuntimeError): e.path_for('new', name)
        with self.assertRaises(ValueError):
            StagedArchiveEndpoints(old, old, trusted_root=out)

    def test_side_selection_add_delete_rename_and_missing_modified_side(self):
        _out, old, new, endpoints = self.staged()
        (old/'before.jar').write_bytes(package(b'OLD_VALUE'))
        for change, calls, old_path in (
                (ChangeType.ADDED, [('new', 'a.jar')], ''),
                (ChangeType.DELETED, [('old', 'a.jar')], ''),
                (ChangeType.RENAMED, [('old', 'before.jar'), ('new', 'a.jar')], 'before.jar')):
            result = self.result(change, old_path=old_path)
            with mock.patch.object(endpoints, 'path_for', wraps=endpoints.path_for) as reader:
                enrich_archive_reports(result, endpoints=endpoints)
                self.assertEqual(calls, [c.args for c in reader.call_args_list])
        (old/'a.jar').unlink()
        with self.assertRaisesRegex(RuntimeError, '旧侧'):
            enrich_archive_reports(self.result(), endpoints=endpoints)

    def folder_task(self):
        old, new = self.root/'old', self.root/'new'
        old.mkdir(); new.mkdir()
        (old/'a.jar').write_bytes(package(b'OLD_VALUE'))
        (new/'a.jar').write_bytes(package(b'NEW_VALUE'))
        return dict(vcs_type='folder', project_name='Demo',
                    old_version=str(old), new_version=str(new))

    def test_no_candidates_do_not_hash_extra_stage_tree(self):
        task = self.folder_task()
        task['exclude_rules'] = '*.jar'
        with mock.patch.object(FileExporter, 'capture_stage_states',
                               side_effect=AssertionError('unnecessary stage scan')):
            app, result, out = self.generate(task)
        self.assert_success(app); self.assertEqual([], result.files)

    def test_analysis_to_commit_rejects_same_size_time_edit_and_replacement(self):
        import main
        task = self.folder_task()
        app, _, out = self.generate(task, enabled=False)
        self.assert_success(app)
        original = tree_hashes(out)
        enrich = main.enrich_archive_reports
        for replace in (False, True):
            def tamper(result, *args, **kwargs):
                answer = enrich(result, *args, **kwargs)
                path = Path(kwargs['endpoints'].path_for('new', 'a.jar'))
                stat = path.stat(); data = bytearray(path.read_bytes())
                data[-1] ^= 1
                if replace:
                    other = path.with_name('replacement.tmp')
                    other.write_bytes(data); os.replace(other, path)
                else:
                    path.write_bytes(data)
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                return answer
            with mock.patch('main.enrich_archive_reports', tamper):
                app, _, _ = self.generate(task, output=out)
            self.assertFalse(app._last_task_record['success'])
            self.assertIn('包内分析', app._show_error.call_args.args[0])
            self.assertEqual({}, tree_hashes(out)); self.assert_no_stages(out)
        app, _, _ = self.generate(task, output=out)
        self.assert_success(app); self.assert_no_stages(out)

    def test_render_and_instruction_failures_do_not_restore_old_outputs(self):
        task = self.folder_task()
        app, _, out = self.generate(task, enabled=False)
        self.assert_success(app); original = tree_hashes(out)
        for target in ('main.ReportGenerator._dump_limited', 'main.prepare_delivery_instructions'):
            with mock.patch(target, side_effect=RuntimeError('injected write failure')):
                app, _, _ = self.generate(task, output=out)
            self.assertFalse(app._last_task_record['success'])
            self.assertEqual({}, tree_hashes(out)); self.assert_no_stages(out)

    def test_bound_stages_publish_failure_and_retry_work(self):
        task = self.folder_task()
        app, _, out = self.generate(task, enabled=False)
        self.assert_success(app); original = tree_hashes(out)
        real_replace = os.rename
        failed = []
        def replace(source, destination):
            if (not failed and '.comparetool_stage_' in str(source)
                    and str(destination) == str(out/'newVersion/Demo')):
                failed.append(True)
                raise OSError('injected install failure')
            return real_replace(source, destination)
        with mock.patch('file_exporter.os.rename', replace):
            app, _, _ = self.generate(task, output=out)
        self.assertTrue(failed); self.assertFalse(app._last_task_record['success'])
        self.assertEqual({}, tree_hashes(out)); self.assert_no_stages(out)
        app, _, _ = self.generate(task, output=out)
        self.assert_success(app); self.assert_no_stages(out)

    def test_lfs_diagnosis_is_bounded_and_exact_and_identical_skips(self):
        _out, old, new, endpoints = self.staged()
        pointer = ('version https://git-lfs.github.com/spec/v1\n'
                   'oid sha256:' + 'a'*64 + '\nsize 42\n').encode()
        (new/'a.jar').write_bytes(pointer)
        self.assertTrue(_is_lfs_pointer(new/'a.jar'))
        with self.assertRaisesRegex(RuntimeError, '新侧.*LFS'):
            enrich_archive_reports(self.result(), endpoints=endpoints)
        (old/'a.jar').write_bytes(pointer)
        result = self.result(ChangeType.RENAMED)
        enrich_archive_reports(result, endpoints=endpoints)
        self.assertEqual('identical', result.files[0].archive_details['status'])
        for data in (pointer + b'x', b'prefix ' + pointer, b'x'*1024, pointer.replace(b'size 42', b'size nope')):
            (new/'a.jar').write_bytes(data)
            self.assertFalse(_is_lfs_pointer(new/'a.jar'))

    def test_intermediate_junction_is_rejected_before_opening_target(self):
        if os.name != 'nt': self.skipTest('Windows junction test')
        out, _old, new, endpoints = self.staged()
        outside = self.root/'outside'; outside.mkdir()
        (outside/'secret.jar').write_bytes(package(b'OUTSIDE'))
        link = new/'link'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True)
        if result.returncode: self.skipTest('当前环境无法创建测试 junction')
        self.addCleanup(lambda: os.rmdir(link) if link.is_dir() else None)
        with mock.patch('archive_endpoints.open_regular_file_no_links',
                        side_effect=AssertionError('must reject before opening')):
            with self.assertRaisesRegex(RuntimeError, '符号链接|联接点'):
                endpoints.path_for('new', 'link/secret.jar')
        self.assertTrue((outside/'secret.jar').exists())

    def test_bound_transaction_windows_lock_release_and_retry(self):
        from path_safety import open_regular_file_no_links
        if os.name != 'nt': self.skipTest('Windows deny-delete semantics')
        task = self.folder_task()
        app, _, out = self.generate(task, enabled=False)
        self.assert_success(app); before = tree_hashes(out)
        with open_regular_file_no_links(str(out/'report.html'), deny_writes=True):
            app, _, _ = self.generate(task, output=out)
            self.assertFalse(app._last_task_record['success'])
        app, _, _ = self.generate(task, output=out)
        self.assert_success(app); self.assert_no_stages(out)
        self.assertEqual(package(b'NEW_VALUE'), (out/'newVersion/Demo/a.jar').read_bytes())

    def test_eight_nested_layers_succeed_and_ninth_fails(self):
        from test_archive_report import zip_bytes
        _out, old, new, endpoints = self.staged()
        def nested(levels, value):
            data = package(value)
            for _ in range(levels-1): data = zip_bytes({'inner.jar': data})
            return data
        for levels in (8, 9):
            (old/'a.jar').write_bytes(nested(levels, b'OLD'))
            (new/'a.jar').write_bytes(nested(levels, b'NEW'))
            if levels == 8:
                enrich_archive_reports(self.result(), endpoints=endpoints)
            else:
                with self.assertRaisesRegex(RuntimeError, '深度'):
                    enrich_archive_reports(self.result(), endpoints=endpoints)
