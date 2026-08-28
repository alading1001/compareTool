import io
import os
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

from delivery_instructions import write_delivery_instructions
from diff_engine import DiffEngine, DiffResult
from file_exporter import FileExporter
from main import CompareToolApp
from path_safety import sanitize_windows_component, split_safe_relative_path
from report_generator import ReportGenerator
from vcs.archive_vcs import ArchiveVCS
from vcs.base import BaseVCS, ChangedFile, ChangeType
from vcs.folder_vcs import FolderVCS
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import SVNMultiVersionVCS
from vcs.svn_vcs import SVNVCS


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(data)


class BytesVCS(BaseVCS):
    def __init__(self, old_data=b"", new_data=b"", change_type=ChangeType.MODIFIED):
        super().__init__("demo")
        self.old_data = old_data
        self.new_data = new_data
        self.change_type = change_type

    def get_changed_files(self, old_version, new_version):
        return [ChangedFile("demo.txt", self.change_type)]

    def get_file_content(self, version, file_path):
        return "fallback"

    def get_file_content_bytes(self, version, file_path):
        return self.old_data if version == "old" else self.new_data

    get_file_content_raw_bytes = get_file_content_bytes
    get_file_content_working = get_file_content

    def get_versions(self):
        return []

    def check_version_exists(self, version):
        return True


class PathAndSnapshotTests(unittest.TestCase):
    def test_windows_short_alias_and_superscript_devices_are_blocked(self):
        with self.assertRaisesRegex(ValueError, "8.3"):
            split_safe_relative_path("dir/LONGFI~1.TXT")
        self.assertEqual("_COM¹", sanitize_windows_component("COM¹"))
        self.assertEqual("_ABC~1.TXT", sanitize_windows_component("ABC~1.TXT"))

    def test_folder_endpoints_snapshot_lazily_then_remain_stable(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            try:
                self.assertEqual([], vcs._owned_temp_dirs)
                write_bytes(os.path.join(new_dir, "value.txt"), b"first read")
                files = vcs.get_changed_files("old", "new")
                self.assertEqual(["value.txt"], [item.path for item in files])
                self.assertEqual(b"first read", vcs.get_file_content_bytes(new_dir, "value.txt"))
                write_bytes(os.path.join(new_dir, "value.txt"), b"later mutation")
                self.assertEqual(b"first read", vcs.get_file_content_bytes(new_dir, "value.txt"))
            finally:
                vcs.cleanup()

    def test_folder_snapshot_does_not_copy_excluded_subtree_files(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "excluded", "large.bin"), b"old excluded")
            write_bytes(os.path.join(new_dir, "excluded", "large.bin"), b"new excluded")
            write_bytes(os.path.join(old_dir, "kept.txt"), b"old")
            write_bytes(os.path.join(new_dir, "kept.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            try:
                vcs.set_exclude_patterns(["excluded/**"])
                files = vcs.get_changed_files("old", "new")
                self.assertEqual(["kept.txt"], [item.path for item in files])
                self.assertFalse(
                    os.path.exists(os.path.join(vcs.old_dir, "excluded", "large.bin"))
                )
                self.assertFalse(
                    os.path.exists(os.path.join(vcs.new_dir, "excluded", "large.bin"))
                )
                self.assertTrue(os.path.isfile(os.path.join(vcs.old_dir, "kept.txt")))
            finally:
                vcs.cleanup()

    def test_folder_snapshot_rejects_missing_source_root(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "missing-old")
            new_dir = os.path.join(root, "new")
            os.makedirs(new_dir)
            vcs = FolderVCS(old_dir, new_dir)
            try:
                with self.assertRaisesRegex(RuntimeError, "源目录不存在"):
                    vcs.get_changed_files("old", "new")
                self.assertEqual([], vcs._owned_temp_dirs)
            finally:
                vcs.cleanup()

    def test_folder_snapshot_rejects_size_change_during_copy(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            original_copy = shutil.copyfileobj

            def copy_then_append(source, target, length):
                original_copy(source, target, length)
                with open(source.name, "ab") as stream:
                    stream.write(b"x")

            try:
                with mock.patch(
                    "vcs.folder_vcs.shutil.copyfileobj", side_effect=copy_then_append
                ):
                    with self.assertRaisesRegex(RuntimeError, "复制期间发生变化"):
                        vcs.get_changed_files("old", "new")
                self.assertEqual([], vcs._owned_temp_dirs)
            finally:
                vcs.cleanup()

    def test_folder_snapshot_rejects_mtime_change_during_copy(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            original_copy = shutil.copyfileobj

            def copy_then_touch(source, target, length):
                original_copy(source, target, length)
                metadata = os.stat(source.name)
                with open(source.name, "r+b") as stream:
                    stream.write(b"X")
                os.utime(
                    source.name,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000),
                )

            try:
                with mock.patch(
                    "vcs.folder_vcs.shutil.copyfileobj", side_effect=copy_then_touch
                ):
                    with self.assertRaisesRegex(RuntimeError, "复制期间发生变化"):
                        vcs.get_changed_files("old", "new")
                self.assertEqual([], vcs._owned_temp_dirs)
            finally:
                vcs.cleanup()

    def test_folder_snapshot_rejects_file_identity_change(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            original_signature = vcs._file_signature
            calls = {}

            def changing_identity(path):
                signature = original_signature(path)
                if path.endswith("value.txt"):
                    calls[path] = calls.get(path, 0) + 1
                    if calls[path] >= 2:
                        return (
                            signature[0], signature[1] + 1, *signature[2:]
                        )
                return signature

            try:
                with mock.patch.object(
                    vcs, "_file_signature", side_effect=changing_identity
                ):
                    with self.assertRaisesRegex(RuntimeError, "复制期间发生变化"):
                        vcs.get_changed_files("old", "new")
                self.assertEqual([], vcs._owned_temp_dirs)
            finally:
                vcs.cleanup()

    def test_folder_snapshot_rejects_path_set_change(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            original_copy = shutil.copyfileobj

            def copy_then_add_path(source, target, length):
                original_copy(source, target, length)
                write_bytes(os.path.join(os.path.dirname(source.name), "added.txt"), b"x")

            try:
                with mock.patch(
                    "vcs.folder_vcs.shutil.copyfileobj", side_effect=copy_then_add_path
                ):
                    with self.assertRaisesRegex(RuntimeError, "路径集合"):
                        vcs.get_changed_files("old", "new")
                self.assertEqual([], vcs._owned_temp_dirs)
            finally:
                vcs.cleanup()

    def test_same_or_nested_folder_endpoints_are_rejected(self):
        with project_temp_dir() as root:
            child = os.path.join(root, "child")
            os.makedirs(child)
            with self.assertRaisesRegex(ValueError, "不能互为祖先"):
                FolderVCS(root, child)

    def test_excluded_replacement_file_cannot_leave_directory_delete_instruction(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "conf", "a.txt"), b"old")
            write_bytes(os.path.join(new_dir, "conf"), b"new file")
            vcs = FolderVCS(old_dir, new_dir)
            try:
                vcs.set_exclude_patterns(["conf", "conf/**"])
                result = DiffEngine(vcs).generate_diff(old_dir, new_dir)
                self.assertEqual([], result.files)
                self.assertEqual([], result.required_directory_deletions)
            finally:
                vcs.cleanup()

    def test_case_only_directory_to_file_replacement_keeps_old_directory_spelling(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "Conf", "a.txt"), b"old")
            write_bytes(os.path.join(new_dir, "conf"), b"new file")
            vcs = FolderVCS(old_dir, new_dir)
            try:
                result = DiffEngine(vcs).generate_diff(old_dir, new_dir)
                self.assertEqual(["Conf"], result.required_directory_deletions)
            finally:
                vcs.cleanup()


class DiffFidelityTests(unittest.TestCase):
    def test_final_newline_only_change_is_visible_as_format_change(self):
        result = DiffEngine(BytesVCS(b"line", b"line\n")).generate_diff("old", "new")
        file_diff = result.files[0]
        self.assertEqual("F", file_diff.report_type)
        self.assertIn("末尾换行", " ".join(file_diff.format_details))
        self.assertIn("末尾换行", file_diff.side_by_side_html)

    def test_utf16_added_file_uses_raw_strict_decode_not_text_fallback(self):
        raw = "你好\n".encode("utf-16")
        result = DiffEngine(
            BytesVCS(b"", raw, ChangeType.ADDED)
        ).generate_diff("old", "new")
        self.assertEqual("你好\n", result.files[0].new_content)

    def test_project_name_is_json_encoded_inside_report_script(self):
        with project_temp_dir() as root:
            result = DiffResult("demo", "</script><script>alert(1)</script>", "Fake", "old", "new")
            report = os.path.join(root, "report.html")
            ReportGenerator().generate(result, report)
            with open(report, encoding="utf-8") as stream:
                html = stream.read()
            self.assertNotIn("</script><script>alert(1)</script>", html)
            self.assertIn("\\u003c/script\\u003e", html)

    def test_diff_line_product_limit_skips_quadratic_work(self):
        with mock.patch.object(DiffEngine, "MAX_TEXT_DIFF_LINE_PRODUCT", 3):
            result = DiffEngine(
                BytesVCS(b"a\nb\n", b"c\nd\n")
            ).generate_diff("old", "new")
        self.assertIn("新旧行数乘积 4", result.files[0].side_by_side_html)
        self.assertIn("仍会完整包含", result.files[0].side_by_side_html)

    def test_line_product_budget_does_not_hide_format_only_semantics(self):
        with mock.patch.object(DiffEngine, "MAX_TEXT_DIFF_LINE_PRODUCT", 3):
            result = DiffEngine(
                BytesVCS(b"a\r\nb\r\n", b"a\nb\n")
            ).generate_diff("old", "new")
        self.assertEqual("F", result.files[0].report_type)
        self.assertIn("仅格式变化", result.files[0].side_by_side_html)

    def test_placeholder_only_files_do_not_retain_aggregate_text(self):
        class PlaceholderOnlyVCS(BytesVCS):
            def __init__(self):
                super().__init__()
                self.contents = {}
                self.changed_files = []
                for index in range(4):
                    path = f"format-{index}.txt"
                    self.changed_files.append(ChangedFile(path, ChangeType.MODIFIED))
                    self.contents[("old", path)] = b"same\r\n" * 100
                    self.contents[("new", path)] = b"same\n" * 100
                for index in range(4):
                    old_path = f"old-{index}.txt"
                    new_path = f"new-{index}.txt"
                    self.changed_files.append(ChangedFile(
                        new_path, ChangeType.RENAMED, old_path=old_path
                    ))
                    self.contents[("old", old_path)] = b"unchanged\n" * 100
                    self.contents[("new", new_path)] = b"unchanged\n" * 100

            def get_changed_files(self, old_version, new_version):
                return list(self.changed_files)

            def get_file_content_bytes(self, version, file_path):
                return self.contents[(version, file_path)]

            get_file_content_raw_bytes = get_file_content_bytes

        with mock.patch.object(DiffEngine, "MAX_REPORT_TEXT_BYTES", 1):
            result = DiffEngine(PlaceholderOnlyVCS()).generate_diff("old", "new")
        self.assertEqual(4, result.summary["format_changed_files"])
        self.assertEqual(4, result.summary["renamed_files"])
        self.assertTrue(all(not item.old_content for item in result.files))
        self.assertTrue(all(not item.new_content for item in result.files))
        self.assertTrue(all(
            "报告展示预算已用尽" not in item.side_by_side_html
            for item in result.files
        ))

    def test_report_aggregate_budget_skips_only_later_rendering(self):
        class TwoFileVCS(BytesVCS):
            def get_changed_files(self, old_version, new_version):
                return [
                    ChangedFile("first.txt", ChangeType.MODIFIED),
                    ChangedFile("second.txt", ChangeType.MODIFIED),
                ]

        cases = (
            ("MAX_REPORT_TEXT_BYTES", 7, "文本总字节"),
            ("MAX_REPORT_TEXT_LINES", 3, "文本总行数"),
            ("MAX_REPORT_RENDER_ROWS", 1, "预计渲染行数"),
        )
        for attribute, limit, reason in cases:
            with self.subTest(attribute=attribute):
                with mock.patch.object(DiffEngine, attribute, limit):
                    result = DiffEngine(
                        TwoFileVCS(b"a\n", b"b\n")
                    ).generate_diff("old", "new")
                self.assertNotIn(
                    "报告展示预算已用尽", result.files[0].side_by_side_html
                )
                self.assertIn(
                    "报告展示预算已用尽", result.files[1].side_by_side_html
                )
                self.assertIn(reason, result.files[1].side_by_side_html)
                self.assertEqual(ChangeType.MODIFIED, result.files[1].change_type)

    def test_directory_deletion_summary_and_templates_are_explicit(self):
        with project_temp_dir() as root:
            result = DiffResult(
                "demo", "demo", "Fake", "old", "new",
                required_directory_deletions=["conf"],
            )
            generator = ReportGenerator()
            self.assertEqual(1, result.summary["required_directory_deletions"])
            self.assertEqual(
                1,
                generator._multi_summary([{"diff_result": result}])[
                    "required_directory_deletions"
                ],
            )
            single = os.path.join(root, "single.html")
            multi = os.path.join(root, "multi.html")
            generator.generate(result, single)
            generator.generate_multi(
                [{
                    "project_name": "demo",
                    "vcs_type": "Fake",
                    "show_project_root": True,
                    "diff_result": result,
                }],
                multi,
            )
            for report in (single, multi):
                with open(report, encoding="utf-8") as stream:
                    rendered = stream.read()
                self.assertIn("需删旧目录", rendered)
                self.assertIn("删除旧目录 1 个", rendered)

    def test_report_trees_use_prototype_safe_directory_maps(self):
        generator = ReportGenerator()
        for template_name in ("report.html", "multi_report.html"):
            source, _filename, _uptodate = generator.env.loader.get_source(
                generator.env, template_name
            )
            self.assertIn("children: Object.create(null)", source)
            self.assertIn(
                "Object.prototype.hasOwnProperty.call(current.children, part)",
                source,
            )


class VCSHardeningTests(unittest.TestCase):
    def test_git_diff_and_later_reads_share_pinned_commit_ids(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = []
        vcs._version_pins = {}
        calls = []

        def run_bytes(args):
            calls.append(args)
            if args[0] == "rev-parse":
                return (("a" if "old" in args[-1] else "b") * 40 + "\n").encode("ascii")
            return b""

        vcs._run_bytes = run_bytes
        vcs.get_changed_files("old", "new")
        diff_call = next(args for args in calls if args[0] == "diff")
        self.assertEqual("a" * 40, diff_call[4])
        self.assertEqual("b" * 40, diff_call[5])
        self.assertEqual("a" * 40, vcs._resolve_version("old"))

    def test_git_export_rejects_active_smudge_or_encoding_attributes(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.get_file_content_raw_bytes = mock.Mock(return_value=b"pointer\n")
        vcs._get_checkout_attributes = mock.Mock(return_value={
            "filter": "lfs", "working-tree-encoding": "utf-16",
        })
        with self.assertRaisesRegex(RuntimeError, "filter=lfs"):
            vcs.get_file_content_bytes("v", "large.bin")

    def test_git_config_errors_other_than_unset_fail_closed(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs._git = "git"
        vcs.project_path = "."
        failed = subprocess.CompletedProcess([], 128, stdout="", stderr="bad config")
        with mock.patch("vcs.git_vcs.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "读取 Git 配置失败"):
                vcs._git_config_value("core.autocrlf")

    def test_svn_log_paths_keep_literal_percent_sequences(self):
        self.assertEqual(
            "/project/name%41.txt",
            SVNMultiVersionVCS._normalize_repo_path("/project/name%41.txt"),
        )

    def test_svn_node_exists_distinguishes_not_found_from_network_failure(self):
        vcs = SVNMultiVersionVCS.__new__(SVNMultiVersionVCS)
        vcs._svn = "svn"
        vcs.source_project_path = "."
        vcs._svn_file_url = lambda version, path: "https://example.invalid/file@1"
        missing = subprocess.CompletedProcess([], 1, b"", b"svn: E160013: not found")
        network = subprocess.CompletedProcess([], 1, b"", b"svn: E170013: network down")
        with mock.patch("vcs.multi_version_vcs.subprocess.run", return_value=missing):
            self.assertFalse(vcs._svn_node_exists("file", 1))
        with mock.patch("vcs.multi_version_vcs.subprocess.run", return_value=network):
            with self.assertRaisesRegex(RuntimeError, "network down"):
                vcs._svn_node_exists("file", 1)

    def test_svn_keywords_fail_even_when_property_value_is_unchanged(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._get_properties = mock.Mock(return_value={"svn:keywords": "Id"})
        with self.assertRaisesRegex(RuntimeError, "svn:keywords"):
            vcs._compare_endpoint_metadata("1", "a.txt", "2", "a.txt")

    def test_svn_directory_to_file_replacement_requires_directory_delete(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs.project_path = "."
        vcs.exclude_patterns = []
        vcs._version_pins = {}
        vcs._pin_source_identity = mock.Mock()
        vcs._pinned_project_url = "https://example.invalid/repo/project"
        vcs._pinned_peg_revision = "2"
        vcs._run = mock.Mock(return_value=(
            '<?xml version="1.0"?><diff><paths>'
            '<path item="replaced" props="none" kind="file">conf</path>'
            '</paths></diff>'
        ))
        vcs._get_node_kind = mock.Mock(return_value="dir")
        vcs._get_properties = mock.Mock(return_value={})
        vcs._compare_endpoint_metadata = mock.Mock(return_value={})
        files = vcs.get_changed_files("1", "2")
        self.assertEqual(ChangeType.ADDED, files[0].change_type)
        self.assertEqual(["conf"], vcs.required_directory_deletions)

    def test_svn_externals_on_changed_directory_fail_closed(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs.project_path = "."
        vcs.exclude_patterns = []
        vcs._version_pins = {}
        vcs._pin_source_identity = mock.Mock()
        vcs._pinned_project_url = "https://example.invalid/repo/project"
        vcs._pinned_peg_revision = "2"
        vcs._run = mock.Mock(return_value=(
            '<?xml version="1.0"?><diff><paths>'
            '<path item="added" props="none" kind="dir">vendor</path>'
            '</paths></diff>'
        ))
        vcs._get_properties = mock.Mock(return_value={"svn:externals": "lib URL"})
        with self.assertRaisesRegex(RuntimeError, "svn:externals"):
            vcs.get_changed_files("1", "2")


class ArchiveAndTransactionTests(unittest.TestCase):
    @staticmethod
    def _write_tar(path, mode):
        with tarfile.open(path, "w") as stream:
            info = tarfile.TarInfo("script.sh")
            info.mode = mode
            info.size = 5
            stream.addfile(info, io.BytesIO(b"echo\n"))

    @staticmethod
    def _pax_record(key, value):
        body = f"{key}={value}\n".encode("utf-8")
        size = len(body) + 2
        while True:
            record = f"{size} ".encode("ascii") + body
            if len(record) == size:
                return record
            size = len(record)

    @staticmethod
    def _write_raw_tar_record(stream, info, payload=b""):
        info.size = len(payload)
        stream.write(info.tobuf())
        stream.write(payload)
        stream.write(b"\0" * ((-len(payload)) % 512))

    @staticmethod
    def _write_zip(path, data, mode=None):
        with zipfile.ZipFile(path, "w") as stream:
            if mode is None:
                stream.writestr("script.sh", data)
            else:
                info = zipfile.ZipInfo("script.sh")
                info.create_system = 3
                info.external_attr = mode << 16
                stream.writestr(info, data)

    def test_archive_mode_only_change_is_reported(self):
        with project_temp_dir() as root:
            old_tar = os.path.join(root, "old.tar")
            new_tar = os.path.join(root, "new.tar")
            self._write_tar(old_tar, 0o644)
            self._write_tar(new_tar, 0o755)
            vcs = ArchiveVCS(old_tar, new_tar)
            try:
                files = vcs.get_changed_files()
                self.assertEqual(1, len(files))
                self.assertEqual(ChangeType.MODIFIED, files[0].change_type)
                self.assertFalse(files[0].old_executable)
                self.assertTrue(files[0].new_executable)
            finally:
                vcs.cleanup()

    def test_oversized_hidden_tar_metadata_is_rejected_before_payload_read(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "bad.tar")
            info = tarfile.TarInfo("pax")
            info.type = tarfile.XHDTYPE
            info.size = ArchiveVCS.MAX_TAR_METADATA_BYTES + 1
            with open(archive, "wb") as stream:
                stream.write(info.tobuf())
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with self.assertRaisesRegex(ValueError, "扩展元数据过大"):
                ArchiveVCS.__new__(ArchiveVCS)._extract_tar(archive, dest)

    def test_cumulative_tar_metadata_limit_rejects_small_headers(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "cumulative-pax.tar")
            payload = self._pax_record("path", "safe.txt")
            with open(archive, "wb") as stream:
                for index in range(2):
                    info = tarfile.TarInfo(f"pax-{index}")
                    info.type = tarfile.XGLTYPE
                    self._write_raw_tar_record(stream, info, payload)
                stream.write(b"\0" * 1024)
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with mock.patch.object(
                ArchiveVCS, "MAX_TAR_METADATA_BYTES", len(payload) * 2 - 1
            ):
                with self.assertRaisesRegex(ValueError, "累计过大"):
                    ArchiveVCS.__new__(ArchiveVCS)._preflight_tar(archive, dest)

    def test_negative_pax_size_is_rejected_before_payload_discard(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "negative-size.tar")
            with open(archive, "wb") as stream:
                pax = tarfile.TarInfo("pax")
                pax.type = tarfile.XHDTYPE
                self._write_raw_tar_record(
                    stream, pax, self._pax_record("size", "-1")
                )
                stream.write(tarfile.TarInfo("safe.txt").tobuf())
                stream.write(b"\0" * 1024)
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with self.assertRaisesRegex(ValueError, "PAX size 无效"):
                ArchiveVCS.__new__(ArchiveVCS)._preflight_tar(archive, dest)

    def test_tar_ratio_limit_is_checked_before_member_body_read(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "ratio.tar")
            info = tarfile.TarInfo("large.bin")
            info.size = 1024
            with open(archive, "wb") as stream:
                stream.write(info.tobuf())
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with mock.patch.object(ArchiveVCS, "MAX_COMPRESSION_RATIO", 1):
                with self.assertRaisesRegex(ValueError, "展开比例过高"):
                    ArchiveVCS.__new__(ArchiveVCS)._preflight_tar(archive, dest)

    def test_zip_member_limit_is_checked_before_zipfile_constructor(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "many.zip")
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("a.txt", b"a")
                stream.writestr("b.txt", b"b")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with mock.patch.object(ArchiveVCS, "MAX_ARCHIVE_MEMBERS", 1), mock.patch(
                "vcs.archive_vcs.zipfile.ZipFile",
                side_effect=AssertionError("ZipFile must not be constructed"),
            ):
                with self.assertRaisesRegex(ValueError, "成员过多"):
                    ArchiveVCS.__new__(ArchiveVCS)._extract_zip(archive, dest)

    def test_zip64_eocd_preflight_accepts_bounded_empty_archive(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "empty-zip64.zip")
            zip64 = struct.pack(
                "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 0, 0, 0, 0
            )
            locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
            eocd = struct.pack(
                "<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF,
                0xFFFFFFFF, 0xFFFFFFFF, 0,
            )
            write_bytes(archive, zip64 + locator + eocd)
            ArchiveVCS._preflight_zip(archive)

    def test_zip_type_zero_unix_mode_change_is_reported(self):
        with project_temp_dir() as root:
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            self._write_zip(old_zip, b"echo\n", 0o644)
            self._write_zip(new_zip, b"echo\n", 0o755)
            vcs = ArchiveVCS(old_zip, new_zip)
            try:
                files = vcs.get_changed_files()
                self.assertEqual(1, len(files))
                self.assertFalse(files[0].old_executable)
                self.assertTrue(files[0].new_executable)
            finally:
                vcs.cleanup()

    def test_archive_inputs_are_snapshotted_before_source_mutation(self):
        with project_temp_dir() as root:
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            self._write_zip(old_zip, b"old")
            self._write_zip(new_zip, b"new")
            vcs = ArchiveVCS(old_zip, new_zip)
            try:
                self._write_zip(new_zip, b"old")
                self.assertEqual(
                    ["script.sh"], [item.path for item in vcs.get_changed_files()]
                )
                self.assertEqual(
                    b"new", vcs.get_file_content_bytes(new_zip, "script.sh")
                )
            finally:
                vcs.cleanup()

    def test_unowned_internal_looking_path_is_not_deleted(self):
        with project_temp_dir() as root:
            path = os.path.join(root, ".comparetool_stage_abcdefgh")
            os.makedirs(path)
            FileExporter._cleanup_orphan_stages(root)
            self.assertTrue(os.path.isdir(path))

    def test_startup_recovery_does_not_write_locks_into_unrelated_directories(self):
        with project_temp_dir() as root:
            unrelated = os.path.join(root, "unrelated-project")
            os.makedirs(unrelated)
            FileExporter.recover_transactions(root, include_direct_children=True)
            self.assertEqual([], os.listdir(unrelated))
            self.assertFalse(os.path.exists(
                os.path.join(root, ".comparetool_transaction.lock")
            ))

    def test_transaction_lock_rejects_second_writer(self):
        with project_temp_dir() as root:
            with FileExporter._transaction_lock(root):
                with self.assertRaisesRegex(RuntimeError, "另一个 CompareTool"):
                    with FileExporter._transaction_lock(root):
                        pass

    def test_archive_source_cannot_be_an_output_target(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "batch", "old.zip")
            write_bytes(archive, b"zip")
            with self.assertRaisesRegex(ValueError, "重叠"):
                CompareToolApp._validate_source_output_separation(
                    [archive], [os.path.join(root, "batch")], []
                )

    def test_directory_delete_instruction_is_explicit(self):
        with project_temp_dir() as root:
            result = DiffResult(
                "demo", "demo", "Fake", "old", "new",
                required_directory_deletions=["conf"],
            )
            output = os.path.join(root, "instructions.txt")
            write_delivery_instructions(
                [{"project_name": "Demo", "diff_result": result}], output
            )
            with open(output, encoding="utf-8-sig") as stream:
                text = stream.read()
            self.assertIn("[删除旧目录后再写入同名文件] Demo/conf", text)


if __name__ == "__main__":
    unittest.main()
