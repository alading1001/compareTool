"""带控制头和十六进制标识的文本：头部、正文及导出字节必须同时保留。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from diff_engine import DiffEngine
from file_exporter import FileExporter
from html_details import HtmlDetailStore
from report_generator import ReportGenerator
from vcs.base import ChangedFile, ChangeType
from test_complete_export_review_fixes import TableRows


OLD_HEADER = bytes.fromhex("03 00 71 0d 02 73 02 76 05 03 77 73 0c 70 07 04") + b"EE83A66AD7BBCC43"
NEW_HEADER = bytes.fromhex("06 0d 07 76 0c 00 06 02 03 76 05 76 03 74 0c 07") + b"4AE3F2403BCCC13E"


class PrefixedVCS:
    project_path = "fixture"
    merge_exact_renames = False

    def __init__(self, old, new, kind=ChangeType.MODIFIED, path="script.js"):
        self.data = {"old": old, "new": new}
        self.kind, self.path = kind, path

    def get_changed_files(self, old, new):
        return [ChangedFile(self.path, self.kind,
                            "before.js" if self.kind == ChangeType.RENAMED else "")]

    def get_file_content_raw_bytes(self, version, path):
        return self.data[version]

    get_file_content_bytes = get_file_content_raw_bytes


class PrefixedTextTests(unittest.TestCase):
    def setUp(self):
        Path(".tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = mock.patch.dict(os.environ, {"COMPARETOOL_TEMP_DIR": str(self.root / "runtime")})
        env.start()
        self.addCleanup(env.stop)

    def test_header_and_body_changes_survive_all_types_context_and_storage(self):
        old_body = '第一行\r\n<jydm>0131</jydm>\r\n末行\r\n'.encode("gbk")
        new_body = '第一行\r\n<jydm>0143</jydm>\r\n末行\r\n'.encode("gbk")
        for kind in (ChangeType.MODIFIED, ChangeType.RENAMED, ChangeType.ADDED, ChangeType.DELETED):
            for full in (False, True):
                for stored in (False, True):
                    with self.subTest(kind=kind, full=full, stored=stored), HtmlDetailStore() as store:
                        vcs = PrefixedVCS(OLD_HEADER + old_body, NEW_HEADER + new_body, kind)
                        result = DiffEngine(vcs, show_full_context=full,
                                            detail_store=store if stored else None,
                                            retain_text_contents=not stored).generate_diff("old", "new")
                        file = result.files[0]
                        rendered = ("".join(file.html_fragment.iter_text()) if stored
                                    else file.side_by_side_html)
                        self.assertIn("特殊头部字节（已变化）", rendered)
                        self.assertIn("header-byte-changed", rendered)
                        self.assertNotIn("二进制归档文件", rendered)
                        self.assertNotIn("\x00", rendered)
                        self.assertTrue(file.line_counts_complete)
                        rows = TableRows(rendered)
                        for new, absent, body in ((False, kind == ChangeType.ADDED, old_body),
                                                  (True, kind == ChangeType.DELETED, new_body)):
                            expected = [] if absent else list(enumerate(body.decode("gbk").splitlines(), 1))
                            self.assertEqual(expected, rows.side(new))
                        self.assertEqual((0, 3) if kind == ChangeType.ADDED else
                                         (3, 0) if kind == ChangeType.DELETED else (1, 1),
                                         (file.deleted_lines, file.added_lines))
                        output = self.root / f"{kind.value}-{full}-{stored}"
                        FileExporter(result, vcs).export(str(output / "old"), str(output / "new"))
                        if kind != ChangeType.ADDED:
                            self.assertEqual(OLD_HEADER + old_body,
                                             (output / "old" / ("before.js" if kind == ChangeType.RENAMED
                                                                 else "script.js")).read_bytes())
                        if kind != ChangeType.DELETED:
                            self.assertEqual(NEW_HEADER + new_body, (output / "new/script.js").read_bytes())
                        generator = ReportGenerator()
                        generator.generate(result, str(output / "single.html"))
                        generator.generate_multi([dict(project_name="demo", vcs_type="folder",
                                                       show_project_root=True, diff_result=result)],
                                                 str(output / "multi.html"))
                        for name in ("single.html", "multi.html"):
                            self.assertIn("特殊头部字节", (output / name).read_text(encoding="utf-8"))

    def test_header_only_change_remains_modified_and_shows_every_byte(self):
        body = "正文完全一致\n".encode("gbk")
        result = DiffEngine(PrefixedVCS(OLD_HEADER + body, NEW_HEADER + body)).generate_diff("old", "new")
        file = result.files[0]
        self.assertEqual("M", file.report_type)
        self.assertFalse(file.format_only)
        self.assertEqual((0, 0), (file.added_lines, file.deleted_lines))
        self.assertIn("正文无变化，特殊头部字节已变化", file.side_by_side_html)
        # 三列表头独立于六列正文表；直接读取两侧十六进制，不依赖 span 分段。
        from html.parser import HTMLParser

        class HeaderCells(HTMLParser):
            def __init__(self, content):
                super().__init__()
                self.cells, self.value, self.inside = [], None, False
                self.feed(content)

            def handle_starttag(self, tag, attrs):
                if tag == "table" and dict(attrs).get("class") == "text-header-bytes":
                    self.inside = True
                elif tag == "td" and self.inside:
                    self.value = []

            def handle_data(self, value):
                if self.value is not None:
                    self.value.append(value)

            def handle_endtag(self, tag):
                if tag == "td" and self.value is not None:
                    self.cells.append("".join(self.value))
                    self.value = None
                elif tag == "table":
                    self.inside = False

        cells = HeaderCells(file.side_by_side_html).cells
        self.assertEqual(["0x0000", "0x0010"], cells[::3])
        self.assertEqual(OLD_HEADER, bytes.fromhex(" ".join(cells[1::3])))
        self.assertEqual(NEW_HEADER, bytes.fromhex(" ".join(cells[2::3])))

    def test_header_added_or_removed_cannot_be_classified_as_format_only(self):
        body = b"same body\n"
        for old, new in ((body, OLD_HEADER + body), (OLD_HEADER + body, body)):
            with self.subTest(old_has_header=old != body):
                file = DiffEngine(PrefixedVCS(old, new)).generate_diff("old", "new").files[0]
                self.assertEqual("M", file.report_type)
                self.assertIn("无特殊头部", file.side_by_side_html)
                self.assertIn("header-byte-changed", file.side_by_side_html)

    def test_same_header_keeps_format_and_pure_rename_semantics(self):
        old = OLD_HEADER + "中文\r\n".encode("gbk")
        new = OLD_HEADER + "中文\n".encode("utf-8")
        file = DiffEngine(PrefixedVCS(old, new)).generate_diff("old", "new").files[0]
        self.assertEqual("F", file.report_type)
        self.assertIn("特殊头部字节（无变化）", file.side_by_side_html)
        self.assertIn("GB18030/GBK → UTF-8", " ".join(file.format_details))
        file = DiffEngine(PrefixedVCS(old, old, ChangeType.RENAMED)).generate_diff("old", "new").files[0]
        self.assertEqual("R", file.report_type)
        self.assertIn("仅重命名，内容无变化", file.side_by_side_html)
        self.assertIn("特殊头部字节", file.side_by_side_html)

    def test_recognition_uses_structure_and_whole_body_not_extension_or_nul_luck(self):
        no_nul_header = OLD_HEADER.replace(b"\x00", b"\x07")
        for path in ("script.js", "script.txt", "script"):
            for header in (OLD_HEADER, no_nul_header):
                with self.subTest(path=path, has_nul=b"\x00" in header):
                    file = DiffEngine(PrefixedVCS(header + b"old\n", header + b"new\n", path=path)).generate_diff("old", "new").files[0]
                    self.assertIn("特殊头部字节", file.side_by_side_html)
                    self.assertEqual([(1, "old")], TableRows(file.side_by_side_html).side())
        for data in (b"plain\x00text\n", OLD_HEADER[:16] + b"invalid-hex-tag!!" + b"text\n",
                     OLD_HEADER + b"text\x00body\n", OLD_HEADER + b"text\n\xff",
                     OLD_HEADER + b"", bytes(range(256)) * 2):
            with self.subTest(data=data[:40]):
                decoded = DiffEngine._decode_text_strict(data)
                self.assertFalse(decoded is not None and decoded.header)
        for codec in ("utf-16", "utf-16-le", "utf-16-be", "utf-32"):
            data = "普通 Unicode 文本\n".encode(codec)
            decoded = DiffEngine._decode_text_strict(data)
            self.assertEqual("普通 Unicode 文本\n", decoded.text)
            self.assertEqual(b"", decoded.header)

    def test_unicode_text_with_header_shaped_bytes_is_never_stripped(self):
        # 合法 UTF-16 中文码点可在字节层恰好组成 16 个 ASCII 十六进制字符。
        middle = b"0123456789ABCDEF".decode("utf-16-le")
        text = "ordinary" + middle + "\n正文\n"
        decoded = DiffEngine._decode_text_strict(text.encode("utf-16-le"))
        self.assertEqual(text, decoded.text)
        self.assertEqual(b"", decoded.header)


if __name__ == "__main__":
    unittest.main()
