import io
import os
import subprocess
import tarfile
import tempfile
import unittest
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

    def test_folder_endpoints_are_snapshotted_before_source_mutation(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            write_bytes(os.path.join(old_dir, "value.txt"), b"old")
            write_bytes(os.path.join(new_dir, "value.txt"), b"new")
            vcs = FolderVCS(old_dir, new_dir)
            try:
                write_bytes(os.path.join(new_dir, "value.txt"), b"old")
                files = vcs.get_changed_files("old", "new")
                self.assertEqual(["value.txt"], [item.path for item in files])
                self.assertEqual(b"new", vcs.get_file_content_bytes(new_dir, "value.txt"))
            finally:
                vcs.cleanup()

    def test_same_or_nested_folder_endpoints_are_rejected(self):
        with project_temp_dir() as root:
            child = os.path.join(root, "child")
            os.makedirs(child)
            with self.assertRaisesRegex(ValueError, "不能互为祖先"):
                FolderVCS(root, child)


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

    def test_unowned_internal_looking_path_is_not_deleted(self):
        with project_temp_dir() as root:
            path = os.path.join(root, ".comparetool_stage_abcdefgh")
            os.makedirs(path)
            FileExporter._cleanup_orphan_stages(root)
            self.assertTrue(os.path.isdir(path))

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
