"""Accuracy boundaries for reusable Windows bindings and lexical path caching."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import path_safety
from vcs.folder_vcs import FolderVCS


class FolderScanFollowupTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(".tmp")
        scratch.mkdir(exist_ok=True)
        self.owned = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.owned.cleanup)
        self.root = Path(self.owned.name).resolve()

    def test_cached_relative_parts_do_not_share_mutable_results_or_policy(self):
        value = "nested/ABC~1.TXT"
        parts = path_safety.split_safe_relative_path(value)
        parts[:] = ["..", "outside"]
        self.assertEqual(["nested", "ABC~1.TXT"], path_safety.split_safe_relative_path(value))
        with self.assertRaisesRegex(ValueError, "8.3"):
            path_safety.split_safe_relative_path(value, reject_short_alias=True)
        for invalid in ("../outside", "a/../../x", "C:/x", "/x", "a:stream", "a/NUL.txt", "a/end."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                path_safety.safe_join(str(self.root), invalid)

    def test_same_relative_path_is_joined_to_the_current_root(self):
        for name in ("old", "OLD", "new"):
            directory = self.root / name
            self.assertEqual(str(directory / "nested" / "file.txt"),
                             path_safety.safe_join(str(directory), "nested/file.txt"))

    def test_walk_keeps_unicode_paths_and_excluded_directory_topology(self):
        source = self.root / "source"
        for relative in ("根文件.txt", "目录 空格/one.txt", "目录 空格/deep/two.txt",
                         "目录 空格/deep/skip.log", "cache/deep/hidden.txt"):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        vcs = FolderVCS(str(source), str(source), snapshot=False)
        self.addCleanup(vcs.cleanup)
        vcs.set_exclude_patterns(["*.log", "cache/**"])
        files, directories = vcs._walk_tree(str(source), apply_excludes=True)
        self.assertEqual({"根文件.txt", "目录 空格/one.txt", "目录 空格/deep/two.txt"}, files)
        self.assertEqual({"目录 空格", "目录 空格/deep", "cache"}, directories)

    def test_final_verification_rejects_same_size_mtime_replacement_of_uncopied_file(self):
        old, new = self.root / "old", self.root / "new"
        for directory in (old, new):
            directory.mkdir()
            (directory / "unchanged.txt").write_bytes(b"same")
            (directory / "changed.txt").write_bytes(directory.name.encode())
        vcs = FolderVCS(str(old), str(new))
        self.addCleanup(vcs.cleanup)
        original = vcs._snapshot_directory
        replacement = self.root / "replacement.txt"
        victim = new / "unchanged.txt"
        metadata = victim.stat()
        replacement.write_bytes(b"evil")
        os.utime(replacement, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

        def snapshot_then_replace(*args, **kwargs):
            result = original(*args, **kwargs)
            if replacement.exists():
                os.replace(replacement, victim)
            return result

        with mock.patch.dict(os.environ, {"COMPARETOOL_TEMP_DIR": str(self.root / "runtime")}), \
                mock.patch.object(vcs, "_snapshot_directory", side_effect=snapshot_then_replace):
            with self.assertRaisesRegex(RuntimeError, "发生变化"):
                vcs.get_changed_files("old", "new")
        self.assertEqual([], vcs._owned_temp_dirs)

    @unittest.skipUnless(os.name == "nt", "Windows handle binding and sharing semantics")
    def test_concurrent_identity_queries_keep_independent_buffers(self):
        paths = [self.root / f"file-{index}.txt" for index in range(8)]
        for index, path in enumerate(paths):
            path.write_bytes(b"x" * (index + 1))
        expected = [path_safety.regular_file_path_identity(str(path)) for path in paths]
        self.assertEqual(len(paths), len({item[0] for item in expected}))

        def query(index):
            with path_safety.open_regular_file_no_links(str(paths[index])) as stream:
                for _ in range(40):
                    self.assertEqual(expected[index], path_safety.regular_file_handle_identity(stream))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(query, range(len(paths))))

    @unittest.skipUnless(os.name == "nt", "Windows exclusive input handles")
    def test_deny_writes_still_blocks_mutation_and_deletion(self):
        path = self.root / "locked.bin"
        path.write_bytes(b"original")
        with path_safety.open_regular_file_no_links(str(path), deny_writes=True) as stream:
            with self.assertRaises(OSError):
                with path.open("r+b") as writer:
                    writer.write(b"changed!")
            with self.assertRaises(OSError):
                path.unlink()
            self.assertEqual(b"original", stream.read())
        path.write_bytes(b"released")

    @unittest.skipUnless(os.name == "nt", "Windows error propagation")
    def test_failed_native_identity_query_still_fails(self):
        path = self.root / "value.txt"
        path.write_bytes(b"x")

        def failed_query(*args):
            path_safety.ctypes.set_last_error(5)
            return 0

        with mock.patch("path_safety._get_file_information", side_effect=failed_query):
            with self.assertRaises(OSError) as failure:
                path_safety.regular_file_path_identity(str(path))
        self.assertEqual(5, failure.exception.winerror)

    @unittest.skipUnless(os.name == "nt", "Windows junction boundary")
    def test_warm_lexical_cache_still_rejects_new_intermediate_junction(self):
        source, outside = self.root / "source", self.root / "outside"
        branch = source / "branch"
        branch.mkdir(parents=True)
        outside.mkdir()
        (branch / "file.txt").write_bytes(b"inside")
        (outside / "file.txt").write_bytes(b"outside")
        vcs = FolderVCS(str(source), str(source), snapshot=False)
        self.addCleanup(vcs.cleanup)
        vcs._resolve_file_path(str(source), "branch/file.txt", check_leaf_link=False)
        branch.rename(source / "saved")
        command = subprocess.run(["cmd", "/c", "mklink", "/J", str(branch), str(outside)],
                                 capture_output=True)
        self.assertEqual(0, command.returncode, command.stderr)
        try:
            with self.assertRaisesRegex(RuntimeError, "越界"):
                vcs._resolve_file_path(str(source), "branch/file.txt", check_leaf_link=False)
        finally:
            os.rmdir(branch)
        self.assertEqual(b"outside", (outside / "file.txt").read_bytes())


if __name__ == "__main__":
    unittest.main()
