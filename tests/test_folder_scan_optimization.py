import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import path_safety
from path_safety import (
    is_link_or_junction,
    metadata_is_link_or_junction,
    regular_file_path_identity,
)
from vcs.folder_vcs import FolderVCS


class PathMetadataFastPathTests(unittest.TestCase):
    def test_metadata_name_surrogate_is_rejected(self):
        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_reparse_tag=0xA000000C,
        )
        self.assertTrue(metadata_is_link_or_junction(metadata))

    @unittest.skipUnless(os.name == "nt", "Windows reparse fast path")
    def test_windows_fast_path_uses_one_lstat_result(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFREG, st_reparse_tag=0)
        with mock.patch("path_safety.os.lstat", return_value=metadata) as lstat:
            with mock.patch("path_safety.os.path.islink",
                            side_effect=AssertionError("legacy islink called")):
                with mock.patch("path_safety.os.path.isjunction",
                                side_effect=AssertionError("legacy isjunction called")):
                    self.assertFalse(is_link_or_junction("C:/normal.txt"))
        lstat.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows handle identity cache")
    def test_path_identity_reuses_initial_handle_identity(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "value.txt"
            path.write_bytes(b"payload")
            original = path_safety.regular_file_handle_identity
            with mock.patch(
                "path_safety.regular_file_handle_identity", wraps=original
            ) as identity:
                result = regular_file_path_identity(str(path))
            self.assertEqual("windows", result[0][0])
            self.assertEqual(1, identity.call_count)


class FolderResolvedPathTests(unittest.TestCase):
    def _bare_vcs(self):
        vcs = FolderVCS.__new__(FolderVCS)
        vcs._real_root_cache = {}
        return vcs

    def test_root_realpath_is_cached_but_target_is_rechecked(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "a.txt"
            path.write_bytes(b"x")
            vcs = self._bare_vcs()
            real = os.path.realpath
            calls = []
            def tracked(value):
                calls.append(os.path.abspath(value))
                return real(value)

            with mock.patch("vcs.folder_vcs.os.path.realpath",
                            side_effect=tracked):
                first = vcs._resolve_file_path(
                    root, "a.txt", check_leaf_link=False
                )
                second = vcs._resolve_file_path(
                    root, "a.txt", check_leaf_link=False
                )
            self.assertEqual(first, second)
            root_abs = os.path.abspath(root)
            file_abs = os.path.abspath(path)
            self.assertEqual(1, calls.count(root_abs))
            self.assertEqual(2, calls.count(file_abs))

    def test_cached_root_does_not_skip_target_escape_check(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "a.txt"
            path.write_bytes(b"x")
            root_abs = os.path.abspath(root)
            vcs = self._bare_vcs()
            vcs._real_root_cache[root_abs] = root_abs
            outside = os.path.abspath(os.path.join(root, "..", "outside.txt"))
            with mock.patch(
                "vcs.folder_vcs.os.path.realpath", return_value=outside
            ):
                with self.assertRaisesRegex(RuntimeError, "越界"):
                    vcs._resolve_file_path(
                        root, "a.txt", check_leaf_link=False
                    )
    def test_capture_defers_file_leaf_check_to_safe_identity_open(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            source.mkdir()
            (source / "a.txt").write_bytes(b"x")
            vcs = FolderVCS.__new__(FolderVCS)
            vcs.exclude_patterns = []
            vcs._real_root_cache = {
                os.path.abspath(source): os.path.realpath(source)
            }
            vcs.MAX_SNAPSHOT_FILES = None
            vcs.MAX_SNAPSHOT_ENTRIES = None
            with mock.patch.object(
                vcs, "_file_signature",
                side_effect=RuntimeError("safe identity rejected leaf"),
            ) as signature:
                with self.assertRaisesRegex(
                    RuntimeError, "safe identity rejected leaf"
                ):
                    vcs._capture_directory(str(source))
            signature.assert_called_once()


if __name__ == "__main__":
    unittest.main()
