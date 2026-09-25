import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from vcs.base import BaseVCS


def windows_error(code):
    error = PermissionError("simulated replacement error")
    error.winerror = code
    return error


@unittest.skipUnless(os.name == "nt", "Windows replacement retry")
class EolStageRetryTests(unittest.TestCase):
    def setUp(self):
        Path(".tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="eol_retry_", dir=".tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / "file.txt"
        self.target.write_bytes(b"a\nb\r\nc\rd")

    def test_success_does_not_sleep(self):
        with mock.patch("vcs.base.os.replace") as replace, \
                mock.patch("vcs.base.time.sleep") as sleep:
            BaseVCS._replace_eol_stage("stage", "target")
        replace.assert_called_once_with("stage", "target")
        sleep.assert_not_called()

    def test_transient_error_retries_same_atomic_operation(self):
        with mock.patch("vcs.base.os.replace", side_effect=[windows_error(5), None]) as replace, \
                mock.patch("vcs.base.time.sleep") as sleep:
            BaseVCS._replace_eol_stage("stage", "target")
        self.assertEqual([mock.call("stage", "target")] * 2, replace.call_args_list)
        sleep.assert_called_once_with(0.05)

    def test_persistent_error_preserves_original_and_propagates(self):
        original = self.target.read_bytes()
        error = windows_error(32)
        with mock.patch("vcs.base.os.replace", side_effect=error) as replace, \
                mock.patch("vcs.base.time.sleep") as sleep:
            with self.assertRaises(PermissionError) as raised:
                BaseVCS._rewrite_file_eol(str(self.target), b"\n")
        self.assertIs(error, raised.exception)
        self.assertEqual(4, replace.call_count)
        self.assertEqual(3, sleep.call_count)
        self.assertEqual(original, self.target.read_bytes())
        self.assertEqual([], list(self.root.glob(".comparetool_eol_*")))

    def test_other_io_errors_are_not_retried(self):
        error = OSError("simulated disk error")
        error.winerror = 112
        with mock.patch("vcs.base.os.replace", side_effect=error) as replace, \
                mock.patch("vcs.base.time.sleep") as sleep:
            with self.assertRaises(OSError):
                BaseVCS._rewrite_file_lf_to_crlf(str(self.target))
        replace.assert_called_once()
        sleep.assert_not_called()

    def test_non_windows_does_not_retry(self):
        replace = mock.Mock(side_effect=windows_error(5))
        with mock.patch("vcs.base.os", SimpleNamespace(name="posix", replace=replace)), \
                mock.patch("vcs.base.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                BaseVCS._replace_eol_stage("stage", "target")
        replace.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
