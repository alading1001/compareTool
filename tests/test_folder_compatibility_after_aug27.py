import os
import shutil
import tempfile
import unittest
from unittest import mock

from path_safety import windows_directories_replaced_by_files
from vcs.base import ChangeType
from vcs.folder_vcs import FolderVCS
from vcs.temp_storage import TEMP_DIR_ENV


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


def write_bytes(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(payload)


def is_within(path, root):
    path = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    root = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == root
    except ValueError:
        return False


class FolderCompatibilityAfterAugust27Tests(unittest.TestCase):
    def test_directory_replacement_returns_only_the_matched_old_prefix(self):
        self.assertEqual(
            ["a", "x"],
            windows_directories_replaced_by_files(
                ["a", "a/b", "x/y", "untouched"],
                ["a", "x", "new/file.txt"],
            ),
        )

    def test_directory_replacement_scan_does_not_build_path_cartesian_product(self):
        count = 50_000
        directories = [f"old/{index}/child" for index in range(count)]
        file_paths = [f"new/{index}" for index in range(count)]

        self.assertEqual(
            [], windows_directories_replaced_by_files(directories, file_paths)
        )

    def test_unicode_casefold_does_not_create_wrong_directory_delete(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "straße", "keep.txt"), b"same")
            write_bytes(os.path.join(old_dir, "straße", "delete.txt"), b"old")
            write_bytes(os.path.join(new_dir, "straße", "keep.txt"), b"same")
            write_bytes(os.path.join(new_dir, "strasse"), b"new file")

            vcs = FolderVCS(old_dir, new_dir)
            try:
                changes = vcs.get_changed_files(old_dir, new_dir)
                self.assertEqual(
                    {"straße/delete.txt", "strasse"},
                    {item.path for item in changes},
                )
                self.assertEqual([], vcs.required_directory_deletions)
            finally:
                vcs.cleanup()

    def test_same_directory_reuses_one_capture_and_returns_no_changes(self):
        with project_temp_dir() as root:
            write_bytes(os.path.join(root, "value.txt"), b"same")
            vcs = FolderVCS(root, os.path.join(root, "."))
            try:
                with mock.patch.object(
                    vcs, "_capture_directory", wraps=vcs._capture_directory
                ) as capture:
                    self.assertEqual(
                        [], vcs.get_changed_files(root, os.path.join(root, "."))
                    )
                capture.assert_called_once_with(root)
                self.assertEqual(vcs.old_dir, vcs.new_dir)
                self.assertFalse(is_within(vcs.old_dir, root))
                self.assertFalse(is_within(vcs.new_dir, root))
            finally:
                vcs.cleanup()

    def test_same_source_uses_file_identity_not_case_normalized_spelling(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")

            # 模拟大小写敏感目录中两个不同 FileId 的路径，其规范化拼写会
            # 碰撞。Python 3.12 的 realpath 内部也会调用 normcase，因此先
            # 固定两条真实解析路径，避免测试替身在 samefile 前篡改输入。
            # 旧实现只比较 normcase 字符串，仍会假成功返回零差异。
            resolved_paths = {
                os.path.abspath(old_dir): os.path.realpath(old_dir),
                os.path.abspath(new_dir): os.path.realpath(new_dir),
            }
            with mock.patch(
                "vcs.folder_vcs.os.path.normcase", return_value="same-spelling"
            ), mock.patch(
                "vcs.folder_vcs.os.path.realpath",
                side_effect=lambda path: resolved_paths[os.path.abspath(path)],
            ):
                vcs = FolderVCS(old_dir, new_dir)
            try:
                changes = vcs.get_changed_files(old_dir, new_dir)
                self.assertEqual(1, len(changes))
                self.assertEqual("value.txt", changes[0].path)
                self.assertEqual(ChangeType.MODIFIED, changes[0].change_type)
            finally:
                vcs.cleanup()

    def test_case_colliding_endpoint_names_keep_separate_snapshot_selections(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "deleted.txt"), b"old")
            write_bytes(os.path.join(new_dir, "added.txt"), b"new")

            # 大小写敏感 Windows 目录中的 old/OLD 可能规范化碰撞，因此两端
            # 待复制集合必须作为各自调用参数传递，不能再经路径字符串字典取回。
            vcs = FolderVCS(old_dir, new_dir)
            try:
                with mock.patch.object(
                    vcs, "_snapshot_directory", wraps=vcs._snapshot_directory
                ) as snapshot:
                    changes = {
                        item.path: item.change_type
                        for item in vcs.get_changed_files(old_dir, new_dir)
                    }
                    self.assertEqual(
                        {
                            "deleted.txt": ChangeType.DELETED,
                            "added.txt": ChangeType.ADDED,
                        },
                        changes,
                    )
                self.assertEqual(
                    {"deleted.txt"},
                    snapshot.call_args_list[0].args[2]["snapshot_files"],
                )
                self.assertEqual(
                    {"added.txt"},
                    snapshot.call_args_list[1].args[2]["snapshot_files"],
                )
                self.assertEqual(
                    b"old", vcs.get_file_content_bytes(old_dir, "deleted.txt")
                )
                self.assertEqual(
                    b"new", vcs.get_file_content_bytes(new_dir, "added.txt")
                )
            finally:
                vcs.cleanup()

    def test_parent_and_child_directories_compare_via_independent_snapshot(self):
        with project_temp_dir() as root:
            parent = os.path.join(root, "parent")
            child = os.path.join(parent, "child")
            write_bytes(os.path.join(parent, "value.txt"), b"old")
            write_bytes(os.path.join(child, "value.txt"), b"new")

            vcs = FolderVCS(parent, child)
            try:
                changes = vcs.get_changed_files(parent, child)
                by_path = {item.path: item.change_type for item in changes}
                self.assertEqual(ChangeType.MODIFIED, by_path["value.txt"])
                self.assertEqual(ChangeType.DELETED, by_path["child/value.txt"])
                self.assertFalse(is_within(vcs.old_dir, parent))
                self.assertFalse(is_within(vcs.new_dir, parent))
                self.assertEqual(
                    b"new", vcs.get_file_content_bytes(child, "value.txt")
                )
            finally:
                vcs.cleanup()

    def test_temp_override_inside_input_is_skipped_for_safe_fallback(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "input", "old")
            new_dir = os.path.join(root, "input", "new")
            configured_inside = os.path.join(old_dir, "temp-cache")
            safe_runtime = os.path.join(root, "safe-runtime")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")

            with mock.patch.dict(
                os.environ, {TEMP_DIR_ENV: configured_inside}
            ), mock.patch(
                "vcs.temp_storage._runtime_base_dir", return_value=safe_runtime
            ):
                vcs = FolderVCS(old_dir, new_dir)
                try:
                    vcs.get_changed_files(old_dir, new_dir)
                    self.assertFalse(os.path.exists(configured_inside))
                    for snapshot in (vcs.old_dir, vcs.new_dir):
                        self.assertFalse(is_within(snapshot, old_dir))
                        self.assertFalse(is_within(snapshot, new_dir))
                finally:
                    vcs.cleanup()

    def test_portable_runtime_candidate_inside_input_is_skipped(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            runtime_inside = os.path.join(new_dir, ".tmp", "comparetool_runtime")
            safe_fallback = os.path.join(root, "safe-fallback")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")

            vcs = FolderVCS(old_dir, new_dir)
            try:
                with mock.patch(
                    "vcs.temp_storage.candidate_temp_roots",
                    return_value=[runtime_inside, safe_fallback],
                ):
                    vcs.get_changed_files(old_dir, new_dir)
                self.assertFalse(os.path.exists(runtime_inside))
                self.assertEqual(
                    os.path.normcase(os.path.abspath(safe_fallback)),
                    os.path.normcase(os.path.dirname(vcs.old_dir)),
                )
                self.assertEqual(
                    os.path.normcase(os.path.abspath(safe_fallback)),
                    os.path.normcase(os.path.dirname(vcs.new_dir)),
                )
            finally:
                vcs.cleanup()

    def test_all_temp_candidates_inside_inputs_fail_explicitly(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            unsafe_candidates = [
                os.path.join(old_dir, "temp"),
                os.path.join(new_dir, "temp"),
            ]

            vcs = FolderVCS(old_dir, new_dir)
            try:
                with mock.patch(
                    "vcs.temp_storage.candidate_temp_roots",
                    return_value=unsafe_candidates,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "比对输入目录之外.*所有候选"
                    ):
                        vcs.get_changed_files(old_dir, new_dir)
            finally:
                vcs.cleanup()

    def test_snapshot_disk_requirement_counts_only_changed_endpoints(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            unchanged = b"x" * 4096
            write_bytes(os.path.join(old_dir, "same.bin"), unchanged)
            write_bytes(os.path.join(new_dir, "same.bin"), unchanged)
            write_bytes(os.path.join(old_dir, "changed.txt"), b"old")
            write_bytes(os.path.join(new_dir, "changed.txt"), b"new")
            usage = type("Usage", (), {"free": 64})()

            vcs = FolderVCS(old_dir, new_dir)
            try:
                with mock.patch(
                    "vcs.folder_vcs.shutil.disk_usage", return_value=usage
                ):
                    changes = vcs.get_changed_files(old_dir, new_dir)
                self.assertEqual(["changed.txt"], [item.path for item in changes])
                self.assertFalse(os.path.exists(os.path.join(vcs.old_dir, "same.bin")))
                self.assertFalse(os.path.exists(os.path.join(vcs.new_dir, "same.bin")))
                self.assertEqual(
                    b"old", vcs.get_file_content_bytes(old_dir, "changed.txt")
                )
                self.assertEqual(
                    b"new", vcs.get_file_content_bytes(new_dir, "changed.txt")
                )
            finally:
                vcs.cleanup()

    def test_mutation_of_uncopied_file_during_snapshot_still_fails(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            old_stable = os.path.join(old_dir, "same.txt")
            write_bytes(old_stable, b"same")
            write_bytes(os.path.join(new_dir, "same.txt"), b"same")
            write_bytes(os.path.join(old_dir, "changed.txt"), b"old")
            write_bytes(os.path.join(new_dir, "changed.txt"), b"new")
            original_copy = shutil.copyfileobj
            mutated = False

            def copy_then_mutate_uncopied(source, target, length):
                nonlocal mutated
                original_copy(source, target, length)
                if not mutated:
                    mutated = True
                    with open(old_stable, "ab") as stream:
                        stream.write(b"!")

            vcs = FolderVCS(old_dir, new_dir)
            try:
                with mock.patch(
                    "vcs.folder_vcs.shutil.copyfileobj",
                    side_effect=copy_then_mutate_uncopied,
                ):
                    with self.assertRaisesRegex(RuntimeError, "快照期间发生变化"):
                        vcs.get_changed_files(old_dir, new_dir)
            finally:
                vcs.cleanup()


if __name__ == "__main__":
    unittest.main()
