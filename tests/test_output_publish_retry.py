"""发布时短暂占用可恢复，等待期间仍拒绝错误目标和变化的暂存内容。"""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from file_exporter import FileExporter
from archive_workflow_fixtures import WorkflowCase
from test_archive_comparison_root import write_archive


def windows_error(code):
    error = OSError("simulated Windows publish failure")
    error.winerror = code
    return error


@contextmanager
def deny_delete(path):
    """持有真实 Windows 文件/目录句柄，允许读写但不共享删除。"""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        close(handle)


@unittest.skipUnless(os.name == "nt", "Windows publish retry")
class OutputPublishRetryTests(unittest.TestCase):
    def setUp(self):
        Path(".tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="publish_retry_", dir=".tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stage = self.root / "stage"
        self.stage.write_bytes(b"complete result")
        self.target = self.root / "batch" / "result.html"
        self.target.parent.mkdir()
        warning = mock.patch("file_exporter.warn")
        self.warning = warning.start()
        self.addCleanup(warning.stop)

    def publish(self, **kwargs):
        FileExporter._replace_outputs([(str(self.stage), str(self.target))],
                                      trusted_root=str(self.root), **kwargs)

    def test_normal_publish_has_no_delay_or_new_content_scan(self):
        with mock.patch("file_exporter.time.sleep") as sleep, \
                mock.patch.object(FileExporter, "_tree_identity", side_effect=AssertionError("extra scan")):
            self.publish()
        sleep.assert_not_called()
        self.warning.assert_not_called()
        self.assertEqual(b"complete result", self.target.read_bytes())

    def test_transient_windows_errors_retry_and_preserve_bytes(self):
        rename = os.rename
        for code in (5, 32, 33):
            with self.subTest(code=code):
                self.stage.write_bytes(b"complete result")
                errors = [windows_error(code), windows_error(code)]
                def retry(source, target):
                    if errors:
                        raise errors.pop()
                    return rename(source, target)
                with mock.patch("file_exporter.os.rename", side_effect=retry), \
                        mock.patch("file_exporter.time.sleep") as sleep:
                    self.publish()
                self.assertEqual(2, sleep.call_count)
                self.assertEqual(b"complete result", self.target.read_bytes())
                self.target.unlink()

    def test_persistent_lock_has_bounded_wait_and_useful_error(self):
        with deny_delete(self.stage), mock.patch("file_exporter.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "无法发布本次输出.*重试 4 次") as raised:
                self.publish()
        self.assertEqual(4, sleep.call_count)
        self.assertAlmostEqual(1.5, sum(call.args[0] for call in sleep.call_args_list))
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn(str(self.target), str(raised.exception))
        self.assertFalse(self.target.exists())
        self.assertEqual(b"complete result", self.stage.read_bytes())

    def test_real_file_and_directory_locks_release_during_wait(self):
        for directory in (False, True):
            with self.subTest(directory=directory), ExitStack() as held:
                if directory:
                    self.stage.mkdir()
                    (self.stage / "value.txt").write_bytes(b"directory result")
                held.enter_context(deny_delete(self.stage))
                with mock.patch("file_exporter.time.sleep", side_effect=lambda _: held.close()) as sleep:
                    self.publish()
                self.assertEqual(1, sleep.call_count)
                actual = self.target / "value.txt" if directory else self.target
                expected = b"directory result" if directory else b"complete result"
                self.assertEqual(expected, actual.read_bytes())
                actual.unlink()
                if directory:
                    self.target.rmdir()

    def test_unrelated_io_errors_fail_without_wait(self):
        for error in (windows_error(112), windows_error(2), windows_error(17), PermissionError("denied")):
            with self.subTest(error=error), \
                    mock.patch("file_exporter.os.rename", side_effect=error) as rename, \
                    mock.patch("file_exporter.time.sleep") as sleep:
                with self.assertRaises(OSError) as raised:
                    self.publish()
                self.assertIs(error, raised.exception)
                rename.assert_called_once()
                sleep.assert_not_called()
        self.assertEqual(b"complete result", self.stage.read_bytes())

    def test_non_windows_does_not_retry(self):
        rename = mock.Mock(side_effect=windows_error(5))
        with mock.patch("file_exporter.os", SimpleNamespace(name="posix", rename=rename)), \
                mock.patch("file_exporter.time.sleep") as sleep:
            with self.assertRaises(OSError):
                FileExporter._rename_output_with_retry("stage", "target", trusted_root="root",
                                                       stage_identity=(1, 2))
        rename.assert_called_once()
        sleep.assert_not_called()

    def test_new_external_target_during_wait_is_preserved(self):
        def create_external(_delay):
            self.target.write_bytes(b"external result")
        with mock.patch("file_exporter.os.rename", side_effect=windows_error(5)) as rename, \
                mock.patch("file_exporter.time.sleep", side_effect=create_external):
            with self.assertRaisesRegex(RuntimeError, "重新创建"):
                self.publish()
        rename.assert_called_once()
        self.assertEqual(b"external result", self.target.read_bytes())
        self.assertEqual(b"complete result", self.stage.read_bytes())

    def test_replaced_stage_during_wait_is_not_published(self):
        replacement = self.root / "replacement"
        replacement.write_bytes(b"foreign content")
        def replace_stage(_delay):
            os.replace(replacement, self.stage)
        with mock.patch("file_exporter.os.rename", side_effect=windows_error(5)) as rename, \
                mock.patch("file_exporter.time.sleep", side_effect=replace_stage):
            with self.assertRaisesRegex(RuntimeError, "暂存项已被替换"):
                self.publish()
        rename.assert_called_once()
        self.assertFalse(self.target.exists())
        self.assertEqual(b"foreign content", self.stage.read_bytes())

    def test_recursive_content_binding_survives_wait(self):
        self.stage.unlink()
        self.stage.mkdir()
        content = self.stage / "member.bin"
        content.write_bytes(b"complete result")
        expected = FileExporter.capture_stage_states([str(self.stage)], trusted_root=str(self.root))
        metadata = content.stat()
        def change_content(_delay):
            content.write_bytes(b"CHANGED! result")
            os.utime(content, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        with mock.patch("file_exporter.os.rename", side_effect=windows_error(5)) as rename, \
                mock.patch("file_exporter.time.sleep", side_effect=change_content):
            with self.assertRaisesRegex(RuntimeError, "身份或内容"):
                self.publish(expected_stage_states=expected)
        rename.assert_called_once()
        self.assertFalse(self.target.exists())

    def test_target_parent_junction_created_during_wait_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        saved = outside / "keep.txt"
        saved.write_bytes(b"unrelated")
        def redirect_parent(_delay):
            self.target.parent.rmdir()
            completed = subprocess.run(["cmd", "/c", "mklink", "/J",
                                        str(self.target.parent), str(outside)], capture_output=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.addCleanup(lambda: os.rmdir(self.target.parent))
        with mock.patch("file_exporter.os.rename", side_effect=windows_error(5)) as rename, \
                mock.patch("file_exporter.time.sleep", side_effect=redirect_parent):
            with self.assertRaisesRegex(RuntimeError, "链接|联接点"):
                self.publish()
        rename.assert_called_once()
        self.assertEqual([saved], list(outside.iterdir()))
        self.assertEqual(b"unrelated", saved.read_bytes())

    def test_later_publish_failure_cleans_only_this_attempt(self):
        next_stage = self.root / "next-stage"
        next_stage.write_bytes(b"next result")
        next_target = self.target.with_name("next.html")
        report_stage = self.root / "report-stage"
        report_stage.write_bytes(b"report")
        report_target = self.target.with_name("report.html")
        unrelated = self.root / "keep.txt"
        unrelated.write_bytes(b"unrelated")
        pairs = [(str(self.stage), str(self.target)), (str(next_stage), str(next_target)),
                 (str(report_stage), str(report_target))]
        with deny_delete(next_stage), mock.patch("file_exporter.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "无法发布本次输出"):
                FileExporter._replace_outputs(pairs, trusted_root=str(self.root))
        self.assertFalse(any(Path(target).exists() for _, target in pairs))
        self.assertEqual(b"unrelated", unrelated.read_bytes())
        self.assertEqual(b"next result", next_stage.read_bytes())
        self.assertEqual(b"report", report_stage.read_bytes())


@unittest.skipUnless(os.name == "nt", "Windows publish retry")
class ArchivePublishRetryWorkflowTests(WorkflowCase):
    def test_zip_and_tar_workers_recover_from_real_publish_lock(self):
        rename = os.rename
        for suffix in (".zip", ".tar"):
            with self.subTest(suffix=suffix), ExitStack() as held:
                old, new = self.root / ("old" + suffix), self.root / ("new" + suffix)
                write_archive(old, {"config.txt": b"OLD_VALUE\n", "data.bin": b"\0OLD"})
                write_archive(new, {"config.txt": b"NEW_VALUE\n", "data.bin": b"\0NEW"})
                task = dict(vcs_type="archive", project_name="Demo",
                            old_version=str(old), new_version=str(new))
                normal, normal_result, normal_out = self.generate(
                    task, enabled=False, output=self.root / ("normal" + suffix))
                self.assert_success(normal)
                injected, errors = [], []
                def lock_first_publish(source, target):
                    if not injected and Path(target).parent.name == "oldVersion":
                        held.enter_context(deny_delete(source))
                        injected.append(True)
                    try:
                        return rename(source, target)
                    except OSError as exc:
                        errors.append(exc.winerror)
                        raise
                with mock.patch("file_exporter.os.rename", side_effect=lock_first_publish), \
                        mock.patch("file_exporter.time.sleep", side_effect=lambda _: held.close()) as sleep:
                    app, result, out = self.generate(
                        task, enabled=False, output=self.root / ("retry" + suffix))
                self.assert_success(app)
                self.assertTrue(injected)
                self.assertTrue(errors and all(code in (5, 32, 33) for code in errors))
                sleep.assert_called_once()
                self.assertEqual(normal_result.summary, result.summary)
                self.assert_same_delivery(normal_out, out)
                self.assertTrue((out / "report.html").is_file())
                self.assert_no_stages(out)


if __name__ == "__main__":
    unittest.main()
