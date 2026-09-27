"""确认覆盖就是删除旧结果；失败不恢复，旧记录不再参与生成。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from diff_engine import DiffResult, FileDiff
from file_exporter import FileExporter
from main import CompareToolApp
from vcs.base import ChangeType


class OutputRegenerationTests(unittest.TestCase):
    def setUp(self):
        Path('.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'output'
        self.output.mkdir()
        self.targets = [self.output / 'oldVersion/Demo', self.output / 'newVersion/Demo',
                        self.output / 'Demo_上线操作说明.txt', self.output / 'Demo_diff.html']
        for p in self.targets[:2]:
            p.mkdir(parents=True)
            (p / 'previous.txt').write_bytes(b'old result')
        for p in self.targets[2:]:
            p.write_bytes(b'old result')

    def session(self):
        return FileExporter.output_session(self.targets, trusted_root=self.output,
                                           directory_targets=self.targets[:2])

    def stages(self):
        result = []
        for i, target in enumerate(self.targets):
            stage = self.output / ('stage' + str(i))
            if i < 2:
                stage.mkdir()
                (stage / 'new.txt').write_bytes(b'new result')
            else:
                stage.write_bytes(b'new result')
            result.append((str(stage), str(target)))
        return result

    def test_confirmed_outputs_are_deleted_before_generation_without_reading_contents(self):
        other = self.output / 'newVersion/Other'
        other.mkdir()
        (other / 'keep.txt').write_bytes(b'other project')
        personal = self.output / 'notes.txt'
        personal.write_bytes(b'personal')
        with mock.patch.object(FileExporter, '_tree_identity', side_effect=AssertionError('read old contents')):
            with self.session() as count:
                self.assertEqual(4, count)
                self.assertTrue(all(not p.exists() for p in self.targets))
                FileExporter._replace_outputs(self.stages(), trusted_root=str(self.output))
        self.assertEqual(b'other project', (other / 'keep.txt').read_bytes())
        self.assertEqual(b'personal', personal.read_bytes())
        self.assertFalse(list(self.output.rglob('*comparetool_backup*')))
        self.assertFalse(list(self.output.glob('.comparetool_transaction_*.json')))

    def test_generation_failure_does_not_restore_previous_results(self):
        with self.assertRaisesRegex(ValueError, 'generation failed'):
            with self.session():
                raise ValueError('generation failed')
        self.assertTrue(all(not p.exists() for p in self.targets))
        # 重试没有 journal 或私钥依赖。
        with self.session() as count:
            self.assertEqual(0, count)
            FileExporter._replace_outputs(self.stages(), trusted_root=str(self.output))
        self.assertEqual(b'new result', self.targets[-1].read_bytes())

    def test_install_failure_removes_only_this_attempt_and_reports_failure(self):
        with self.session():
            pairs = self.stages()
            rename = os.rename
            def fail(source, target):
                if str(target) == str(self.targets[1]):
                    raise PermissionError('file locked')
                return rename(source, target)
            with mock.patch('file_exporter.os.rename', side_effect=fail):
                with self.assertRaisesRegex(PermissionError, 'file locked'):
                    FileExporter._replace_outputs(pairs, trusted_root=str(self.output))
            self.assertTrue(all(not p.exists() for p in self.targets))

    def test_delete_failure_does_not_start_generation_and_releases_lock(self):
        with mock.patch.object(FileExporter, '_remove_path', side_effect=PermissionError('locked')):
            with self.assertRaisesRegex(RuntimeError, '无法删除上次输出'):
                with self.session():
                    self.fail('must not generate')
        self.assertTrue(all(p.exists() for p in self.targets))
        with self.session():
            pass

    def test_old_unsigned_corrupt_and_signed_records_and_backup_are_left_untouched(self):
        paths = []
        for name in ('.comparetool_transaction_' + 'a' * 32 + '.json',
                     '.comparetool_transaction_' + 'a' * 32 + '.json.commit',
                     'private.key'):
            p = self.output / name
            p.write_bytes(b'invalid old record')
            paths.append(p)
        backup = self.output / 'newVersion/Demo.comparetool_backup_old'
        backup.mkdir()
        (backup / 'saved.txt').write_bytes(b'saved')
        with self.session():
            FileExporter._replace_outputs(self.stages(), trusted_root=str(self.output))
        self.assertTrue(all(p.read_bytes() == b'invalid old record' for p in paths))
        self.assertEqual(b'saved', (backup / 'saved.txt').read_bytes())

    def test_whole_root_and_overlapping_targets_are_rejected_before_deletion(self):
        for targets in ([self.output], [self.targets[0], self.targets[0] / 'previous.txt'],
                        [self.targets[2], self.targets[2]]):
            with self.subTest(targets=targets), self.assertRaises(RuntimeError):
                with FileExporter.output_session(targets, trusted_root=self.output):
                    self.fail('unsafe target')
        self.assertTrue(all(p.exists() for p in self.targets))

    def test_type_mismatch_preserves_every_target_before_deletion(self):
        with self.assertRaisesRegex(RuntimeError, '类型不符'):
            with FileExporter.output_session(self.targets, trusted_root=self.output):
                self.fail('directories were not declared')
        self.assertTrue(all(p.exists() for p in self.targets))

    def test_same_batch_second_writer_is_rejected_before_touching_first_writer(self):
        with self.session():
            self.targets[-1].write_bytes(b'first writer')
            with self.assertRaisesRegex(RuntimeError, '另一个 CompareTool'):
                with self.session():
                    self.fail('concurrent writer')
            self.assertEqual(b'first writer', self.targets[-1].read_bytes())

    def test_external_result_created_during_generation_is_not_deleted(self):
        with self.session():
            pairs = self.stages()
            self.targets[-1].write_bytes(b'user created result')
            with self.assertRaisesRegex(RuntimeError, '重新创建'):
                FileExporter._replace_outputs(pairs, trusted_root=str(self.output))
            self.assertTrue(all(Path(s).exists() for s, _t in pairs))
        self.assertEqual(b'user created result', self.targets[-1].read_bytes())

    def test_recursive_stage_content_change_still_fails(self):
        with self.session():
            pairs = self.stages()
            stage = Path(pairs[0][0])
            expected = FileExporter.capture_stage_states([str(stage)], trusted_root=str(self.output))
            file = stage / 'new.txt'
            before = file.stat()
            file.write_bytes(b'bad result')
            os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaisesRegex(RuntimeError, '身份或内容'):
                FileExporter._replace_outputs(pairs, trusted_root=str(self.output), expected_stage_states=expected)
        self.assertTrue(all(not p.exists() for p in self.targets))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_child_junction_deletion_does_not_touch_external_content(self):
        external = self.root / 'external'
        external.mkdir()
        saved = external / 'keep.txt'
        saved.write_bytes(b'must survive')
        junction = self.targets[0] / 'link'
        process = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(external)],
                                 capture_output=True)
        self.assertEqual(0, process.returncode, process.stderr)
        with self.session():
            pass
        self.assertEqual(b'must survive', saved.read_bytes())

    def test_real_worker_deletes_before_comparison_and_failure_leaves_no_report(self):
        old, new = self.root / 'input-old', self.root / 'input-new'
        old.mkdir()
        new.mkdir()
        app = CompareToolApp.__new__(CompareToolApp)
        app.root = mock.Mock()
        def fail(*args):
            self.assertTrue(all(not p.exists() for p in self.targets))
            raise RuntimeError('comparison failed after clear')
        with mock.patch('main.DiffEngine.generate_diff', side_effect=fail):
            app._do_generate(str(new), 'folder', str(old), str(new), 'Demo', [], True, True,
                             str(self.targets[-1]), str(self.output / 'oldVersion'),
                             str(self.output / 'newVersion'), str(self.output))
        self.assertFalse(app._last_task_record['success'])
        self.assertTrue(all(not p.exists() for p in self.targets))


if __name__ == '__main__':
    unittest.main()
