import os
import tempfile
import unittest

from diff_engine import DiffEngine
from file_exporter import FileExporter
from report_generator import ReportGenerator
from vcs.base import ChangedFile, ChangeType


class FakeVCS:
    project_path = "demo"

    def __init__(self, old_bytes: bytes, new_bytes: bytes, path: str = "demo.txt"):
        self.old_bytes = old_bytes
        self.new_bytes = new_bytes
        self.path = path

    def get_changed_files(self, old_version, new_version):
        return [ChangedFile(self.path, ChangeType.MODIFIED)]

    def get_file_content_raw_bytes(self, version, file_path):
        return self.old_bytes if version == "old" else self.new_bytes

    def get_file_content_bytes(self, version, file_path):
        return self.get_file_content_raw_bytes(version, file_path)

    def get_file_content(self, version, file_path):
        data = self.get_file_content_raw_bytes(version, file_path)
        for encoding in ("utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", errors="replace")

    def get_file_content_working(self, file_path):
        return self.get_file_content("new", file_path)


class FormatChangeTests(unittest.TestCase):
    def generate(self, old_bytes, new_bytes, path="demo.txt"):
        return DiffEngine(FakeVCS(old_bytes, new_bytes, path)).generate_diff("old", "new")

    def test_encoding_only_change_is_separate_but_remains_modified_underneath(self):
        result = self.generate("中文内容\n".encode("gbk"), "中文内容\n".encode("utf-8"))

        file_diff = result.files[0]
        self.assertEqual(ChangeType.MODIFIED, file_diff.change_type)
        self.assertEqual("F", file_diff.report_type)
        self.assertTrue(file_diff.format_only)
        self.assertIn("编码：GB18030/GBK → UTF-8", file_diff.format_details)
        self.assertEqual(0, file_diff.added_lines)
        self.assertEqual(0, file_diff.deleted_lines)
        self.assertEqual(1, result.summary["format_changed_files"])
        self.assertEqual(0, result.summary["modified_files"])

    def test_line_ending_only_change_is_format_change(self):
        result = self.generate(b"first\r\nsecond\r\n", b"first\nsecond\n")

        self.assertEqual("F", result.files[0].report_type)
        self.assertIn("换行符：CRLF → LF", result.files[0].format_details)

    def test_bom_only_change_is_format_change(self):
        result = self.generate("内容".encode("utf-8"), b"\xef\xbb\xbf" + "内容".encode("utf-8"))

        self.assertEqual("F", result.files[0].report_type)
        self.assertIn("BOM：无 → UTF-8 BOM", result.files[0].format_details)

    def test_real_content_change_with_same_line_count_stays_modified(self):
        result = self.generate("旧内容\n".encode("utf-8"), "新内容\n".encode("utf-8"))

        file_diff = result.files[0]
        self.assertEqual("M", file_diff.report_type)
        self.assertFalse(file_diff.format_only)
        self.assertEqual(1, file_diff.added_lines)
        self.assertEqual(1, file_diff.deleted_lines)
        self.assertEqual(1, result.summary["modified_files"])
        self.assertEqual(0, result.summary["format_changed_files"])

    def test_empty_file_and_single_line_feed_are_real_content_changes(self):
        for old_bytes, new_bytes in ((b"", b"\n"), (b"\n", b"")):
            with self.subTest(old=old_bytes, new=new_bytes):
                result = self.generate(old_bytes, new_bytes)
                file_diff = result.files[0]

                self.assertEqual("M", file_diff.report_type)
                self.assertFalse(file_diff.format_only)
                self.assertEqual(1, file_diff.added_lines + file_diff.deleted_lines)

    def test_renamed_empty_file_to_single_line_feed_is_not_format_only(self):
        class RenameVCS(FakeVCS):
            def get_changed_files(self, old_version, new_version):
                return [ChangedFile(
                    "new.txt", ChangeType.RENAMED, old_path="old.txt"
                )]

        result = DiffEngine(RenameVCS(b"", b"\n")).generate_diff(
            "old", "new"
        )
        file_diff = result.files[0]
        self.assertEqual("R", file_diff.report_type)
        self.assertFalse(file_diff.format_only)
        self.assertEqual(1, file_diff.added_lines)
        self.assertEqual(0, file_diff.deleted_lines)

    def test_undecodable_change_is_not_guessed_as_format_only(self):
        result = self.generate(b"\x81", b"\x82")

        self.assertEqual("M", result.files[0].report_type)
        self.assertFalse(result.files[0].format_only)

    def test_binary_extension_keeps_existing_binary_behavior(self):
        result = self.generate(b"old\x00", b"new\x00", "library.dll")

        self.assertEqual("M", result.files[0].report_type)
        self.assertIn("二进制归档文件", result.files[0].side_by_side_html)

    def test_single_report_uses_format_display_type(self):
        result = self.generate("中文".encode("gbk"), "中文".encode("utf-8"))
        generator = ReportGenerator()
        rendered = generator.env.get_template("report.html").render(
            project_name="demo",
            project_path="demo",
            vcs_type="FakeVCS",
            old_version="old",
            new_version="new",
            summary=result.summary,
            files=result.files,
            show_project_root=True,
            generated_at="2026-07-15 00:00:00",
        )

        self.assertIn('type: "F"', rendered)
        self.assertIn("格式变化文件", rendered)
        self.assertIn("仅格式变化，文字内容无变化", rendered)

    def test_multi_report_uses_format_display_type(self):
        result = self.generate("中文".encode("gbk"), "中文".encode("utf-8"))
        generator = ReportGenerator()
        rendered = generator.env.get_template("multi_report.html").render(
            summary=generator._multi_summary([{"diff_result": result}]),
            projects=[{
                "project_name": "demo",
                "vcs_type": "FakeVCS",
                "show_project_root": True,
                "diff_result": result,
            }],
            generated_at="2026-07-15 00:00:00",
        )

        self.assertIn('type: "F"', rendered)
        self.assertIn("格式变化文件", rendered)

    def test_format_only_file_is_still_exported_as_modified(self):
        vcs = FakeVCS("中文".encode("gbk"), "中文".encode("utf-8"))
        result = DiffEngine(vcs).generate_diff("old", "new")

        class CapturingExporter(FileExporter):
            def __init__(self, diff_result, source_vcs):
                super().__init__(diff_result, source_vcs)
                self.writes = []

            def _write_file(self, base_dir, rel_path, version, text_content):
                self.writes.append((base_dir, rel_path, version))

        exporter = CapturingExporter(result, vcs)
        os.makedirs(".tmp", exist_ok=True)
        with tempfile.TemporaryDirectory(dir=".tmp") as tmp_dir:
            old_out = os.path.join(tmp_dir, "old-out")
            new_out = os.path.join(tmp_dir, "new-out")
            exporter.export(old_out, new_out)

            self.assertEqual([
                ("demo.txt", "old"),
                ("demo.txt", "new"),
            ], [(rel_path, version) for _, rel_path, version in exporter.writes])


if __name__ == "__main__":
    unittest.main()
