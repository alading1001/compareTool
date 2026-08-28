import os
import subprocess
import tempfile
import unittest
from unittest import mock

from diff_engine import DiffEngine, DiffResult, FileDiff
from file_exporter import FileExporter
from report_generator import ReportGenerator
from vcs.archive_vcs import ArchiveVCS
from vcs.base import BaseVCS, ChangedFile, ChangeType
from vcs.folder_vcs import FolderVCS
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS, _HistoryChange


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(data)


class SizedBytesVCS(BaseVCS):
    def __init__(self, old_data, new_data):
        super().__init__("demo")
        self.old_data = old_data
        self.new_data = new_data

    def get_changed_files(self, old_version, new_version):
        return [ChangedFile("demo.txt", ChangeType.MODIFIED)]

    def get_file_size(self, version, file_path):
        data = self.old_data if version == "old" else self.new_data
        return None if data is None else len(data)

    def get_file_content_raw_bytes(self, version, file_path):
        return self.old_data if version == "old" else self.new_data

    get_file_content_bytes = get_file_content_raw_bytes

    def get_file_content(self, version, file_path):
        data = self.old_data if version == "old" else self.new_data
        return "" if data is None else data.decode("utf-8", errors="replace")

    get_file_content_working = get_file_content

    def get_versions(self):
        return []

    def check_version_exists(self, version):
        return True


class DiffAndReportBudgetTests(unittest.TestCase):
    def test_directory_replacement_inference_is_prefix_linear_and_not_exact_file(self):
        changes = [
            ChangedFile(f"tree/{index}/child.txt", ChangeType.DELETED)
            for index in range(20_000)
        ]
        changes.extend([
            ChangedFile("tree/19999", ChangeType.ADDED),
            ChangedFile("same.txt", ChangeType.DELETED),
            ChangedFile("same.txt", ChangeType.ADDED),
        ])

        required = DiffEngine._infer_required_directory_deletions(changes, [])

        self.assertIn("tree/19999", required)
        self.assertNotIn("same.txt", required)

    def test_combined_line_and_character_work_skips_html_diff(self):
        old_data = (b"abcdefghij\n" * 100)
        new_data = (b"jihgfedcba\n" * 100)
        engine = DiffEngine(SizedBytesVCS(old_data, new_data))
        engine.MAX_TEXT_DIFF_COMBINED_WORK = 1_000
        with mock.patch.object(
            __import__("difflib").HtmlDiff,
            "make_table",
            side_effect=AssertionError("HtmlDiff must not run"),
        ):
            result = engine.generate_diff("old", "new")
        self.assertIn("组合工作量", result.files[0].side_by_side_html)

    def test_known_size_does_not_turn_missing_raw_endpoint_into_empty_success(self):
        class MissingRaw(SizedBytesVCS):
            def get_file_size(self, version, file_path):
                return 1

        with self.assertRaisesRegex(RuntimeError, "无法读取旧版本原始字节"):
            DiffEngine(MissingRaw(None, b"x")).generate_diff("old", "new")

    def test_raw_endpoint_size_mismatch_fails_closed(self):
        class Mismatch(SizedBytesVCS):
            def get_file_size(self, version, file_path):
                return 99

        with self.assertRaisesRegex(RuntimeError, "大小与读取字节不一致"):
            DiffEngine(Mismatch(b"old", b"new")).generate_diff("old", "new")

    def test_summary_manifest_includes_files_omitted_from_detail(self):
        files = [
            FileDiff(
                f"src/file_{index:05d}.txt",
                ChangeType.MODIFIED,
                report_detail_omitted=index >= 5_000,
            )
            for index in range(6_001)
        ]
        result = DiffResult("demo", "demo", "Fake", "old", "new", files)
        with project_temp_dir() as root:
            report = os.path.join(root, "report.html")
            ReportGenerator().generate(result, report)
            with open(report, encoding="utf-8") as stream:
                html = stream.read()
        self.assertIn("src/file_06000.txt", html)
        self.assertIn("当前列出", html)

    def test_manifest_cap_is_explicit_in_report(self):
        files = [FileDiff(f"f{index}.txt", ChangeType.ADDED) for index in range(3)]
        result = DiffResult("demo", "demo", "Fake", "old", "new", files)
        with project_temp_dir() as root, mock.patch.object(
            DiffResult, "MAX_REPORT_MANIFEST_FILES", 2
        ):
            report = os.path.join(root, "report.html")
            ReportGenerator().generate(result, report)
            with open(report, encoding="utf-8") as stream:
                html = stream.read()
        self.assertIn("仅列出 2 / 3", html)


class GitHistoryResourceTests(unittest.TestCase):
    @staticmethod
    def _bare_multi():
        vcs = GitMultiVersionVCS.__new__(GitMultiVersionVCS)
        vcs.exclude_patterns = []
        vcs._git_pair_candidates = 0
        vcs._git_scoring_evaluations = 0
        vcs._git_scoring_bytes = 0
        vcs._git_stored_ambiguous_candidates = 0
        vcs._git_rename_candidate_cache = {}
        return vcs

    def test_git_history_candidate_matrix_is_bounded_before_cartesian_set(self):
        vcs = self._bare_multi()
        changes = [
            *(_HistoryChange("D", f"old/{i}.txt") for i in range(225)),
            *(_HistoryChange("A", f"new/{i}.txt") for i in range(225)),
        ]
        vcs._diff_changes = lambda commit, parent, threshold: list(changes)
        with self.assertRaisesRegex(RuntimeError, "矩阵过多"):
            vcs._changes_for_commit("b" * 40, "a" * 40)

    def test_git_history_excludes_apply_at_each_rename_boundary(self):
        vcs = self._bare_multi()
        vcs.exclude_patterns = ["secret/**"]
        filtered = vcs._filter_git_history_changes([
            _HistoryChange("R", "kept/from_secret.txt", "secret/a.txt"),
            _HistoryChange("R", "secret/b.txt", "kept/to_secret.txt"),
            _HistoryChange("R", "secret/d.txt", "secret/c.txt"),
            _HistoryChange("M", "secret/noise.txt"),
        ])
        self.assertEqual([
            _HistoryChange("A", "kept/from_secret.txt"),
            _HistoryChange("D", "kept/to_secret.txt"),
        ], filtered)

    def test_git_rename_scoring_streams_blobs_instead_of_loading_them(self):
        class StreamingContent:
            def __init__(self):
                self.exports = []

            def get_file_size(self, version, path):
                return 4

            def get_file_content_raw_bytes(self, version, path):
                raise AssertionError("whole blob getter must not be used")

            def export_raw_file_to_path(self, version, path, target):
                self.exports.append((version, path))
                write_bytes(target, b"same")

        with project_temp_dir() as root:
            vcs = self._bare_multi()
            vcs._content_vcs = StreamingContent()
            vcs._git_exe = "git"
            vcs._tmp_root = root
            vcs.source_project_path = root
            result = subprocess.CompletedProcess([], 1, b"R100\x00source\x00target\x00", b"")
            with mock.patch("vcs.multi_version_vcs.subprocess.run", return_value=result):
                self.assertTrue(vcs._git_rename_candidate("old", "a", "new", "b"))
            self.assertEqual([("old", "a"), ("new", "b")], vcs._content_vcs.exports)

    def test_normal_git_refs_are_resolved_as_a_stable_group(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs._version_pins = {}
        values = [b"a" * 40, b"b" * 40, b"a" * 40, b"b" * 40]
        calls = []

        def run_bytes(args):
            calls.append(args[-1])
            return values.pop(0) + b"\n"

        vcs._run_bytes = run_bytes
        pinned = vcs._pin_versions_stable(("old", "new"))
        self.assertEqual({"old": "a" * 40, "new": "b" * 40}, pinned)
        self.assertEqual([
            "old^{commit}", "new^{commit}", "old^{commit}", "new^{commit}"
        ], calls)


class SnapshotAndTransactionTests(unittest.TestCase):
    def test_folder_sources_are_both_captured_before_either_copy(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            original = vcs._snapshot_directory

            def snapshot(source, prefix, capture, required):
                result = original(source, prefix, capture, required)
                if os.path.abspath(source) == os.path.abspath(old_dir):
                    write_bytes(os.path.join(new_dir, "late.txt"), b"late")
                return result

            vcs._snapshot_directory = snapshot
            try:
                with self.assertRaisesRegex(RuntimeError, "路径集合.*发生变化"):
                    vcs.get_changed_files("old", "new")
            finally:
                vcs.cleanup()

    def test_folder_snapshot_file_count_and_disk_space_are_bounded(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "a.txt"), b"a")
            write_bytes(os.path.join(new_dir, "b.txt"), b"b")
            with mock.patch.object(FolderVCS, "MAX_SNAPSHOT_FILES", 1):
                vcs = FolderVCS(old_dir, new_dir)
                try:
                    with self.assertRaisesRegex(RuntimeError, "文件数超过上限"):
                        vcs.get_changed_files("old", "new")
                finally:
                    vcs.cleanup()

            vcs = FolderVCS(old_dir, new_dir)
            try:
                usage = type("Usage", (), {"free": 0})()
                with mock.patch("vcs.folder_vcs.shutil.disk_usage", return_value=usage):
                    with self.assertRaisesRegex(RuntimeError, "磁盘空间不足"):
                        vcs.get_changed_files("old", "new")
            finally:
                vcs.cleanup()

    def test_archive_source_snapshots_have_a_total_size_cap(self):
        with project_temp_dir() as root:
            old_archive = os.path.join(root, "old.zip")
            new_archive = os.path.join(root, "new.zip")
            write_bytes(old_archive, b"not-a-real-zip")
            write_bytes(new_archive, b"not-a-real-zip")
            with mock.patch.object(ArchiveVCS, "MAX_ARCHIVE_SOURCE_BYTES", 1):
                with self.assertRaisesRegex(RuntimeError, "源文件总大小超过上限"):
                    ArchiveVCS(old_archive, new_archive)

    def test_ancestor_link_check_aborts_before_replacing_target(self):
        with project_temp_dir() as root:
            stage = os.path.join(root, "stage.txt")
            target = os.path.join(root, "batch", "oldVersion", "target.txt")
            write_bytes(stage, b"staged")
            write_bytes(target, b"original")
            real_check = __import__("path_safety").ensure_no_link_components

            def guarded(check_root, path, label="path"):
                if os.path.abspath(path) == os.path.abspath(target):
                    raise ValueError("ancestor junction")
                return real_check(check_root, path, label)

            with mock.patch("file_exporter.ensure_no_link_components", side_effect=guarded):
                with self.assertRaisesRegex(RuntimeError, "ancestor junction"):
                    FileExporter._replace_outputs([(stage, target)])
            with open(target, "rb") as stream:
                self.assertEqual(b"original", stream.read())

    def test_optimistic_target_state_prevents_stale_writer_overwrite(self):
        with project_temp_dir() as root:
            target = os.path.join(root, "output")
            stage = os.path.join(root, ".comparetool_stage_stale")
            write_bytes(os.path.join(target, "value.txt"), b"original")
            write_bytes(os.path.join(stage, "value.txt"), b"stale")
            expected = FileExporter.capture_target_states([target])
            write_bytes(os.path.join(target, "value.txt"), b"newer")

            with self.assertRaisesRegex(RuntimeError, "生成期间已被.*修改"):
                FileExporter._replace_outputs(
                    [(stage, target)], expected_target_states=expected
                )
            with open(os.path.join(target, "value.txt"), "rb") as stream:
                self.assertEqual(b"newer", stream.read())

    def test_recovery_identity_mismatch_preserves_user_replacement(self):
        with project_temp_dir() as root:
            token = "f" * 32
            target = os.path.join(root, "output")
            stage = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=root)
            write_bytes(os.path.join(target, "value.txt"), b"original")
            write_bytes(os.path.join(stage, "value.txt"), b"staged")
            state = {
                "stage": stage,
                "target": target,
                "backup": f"{target}.comparetool_backup_{token}",
                "had_target": True,
                "installed": False,
            }
            journal = FileExporter._create_transaction_journal([state], token)
            os.replace(target, state["backup"])
            os.replace(stage, target)
            write_bytes(os.path.join(target, "value.txt"), b"user replacement")

            with self.assertRaisesRegex(RuntimeError, "身份或内容元数据已变化"):
                FileExporter.recover_transactions(root, raise_on_error=True)
            with open(os.path.join(target, "value.txt"), "rb") as stream:
                self.assertEqual(b"user replacement", stream.read())
            self.assertTrue(os.path.exists(state["backup"]))
            self.assertTrue(os.path.exists(journal))


if __name__ == "__main__":
    unittest.main()
