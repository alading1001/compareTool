import os
import random
import tempfile
import unittest
from unittest import mock

from diff_engine import DiffEngine
from report_generator import ReportGenerator
from vcs.base import BaseVCS, ChangedFile, ChangeType


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


class _BytesVCS:
    project_path = "demo"
    merge_exact_renames = False

    def __init__(self, old_data, new_data, path="demo.txt"):
        self.old_data = old_data
        self.new_data = new_data
        self.path = path

    def get_changed_files(self, old_version, new_version):
        return [ChangedFile(self.path, ChangeType.MODIFIED)]

    def get_file_content_raw_bytes(self, version, file_path):
        return self.old_data if version == "old" else self.new_data

    get_file_content_bytes = get_file_content_raw_bytes

    def get_file_content(self, version, file_path):
        data = self.get_file_content_raw_bytes(version, file_path)
        for encoding in ("utf-8", "gb18030"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", errors="replace")


class _LegacyVCS(BaseVCS):
    def __init__(self, content):
        super().__init__("demo")
        self.content = content

    def get_changed_files(self, old_version, new_version):
        return []

    def get_file_content(self, version, file_path):
        return self.content

    def get_file_content_working(self, file_path):
        return self.content

    def get_versions(self):
        return []

    def check_version_exists(self, version):
        return True


class TextClassificationCompatibilityTests(unittest.TestCase):
    def test_each_text_endpoint_is_decoded_once_and_not_read_again(self):
        vcs = _BytesVCS(b"old\n", b"new\n")
        vcs.get_file_content = mock.Mock(
            side_effect=AssertionError("strictly decoded endpoint must be reused")
        )
        vcs.get_file_raw_size = mock.Mock(
            side_effect=AssertionError("disabled size cap must not probe VCS")
        )
        decoder = DiffEngine._decode_text_strict

        with mock.patch.object(
            DiffEngine, "_decode_text_strict", wraps=decoder
        ) as decode:
            result = DiffEngine(vcs).generate_diff("old", "new")

        self.assertIn('<table class="diff"', result.files[0].side_by_side_html)
        self.assertEqual(2, decode.call_count)
        vcs.get_file_content.assert_not_called()
        vcs.get_file_raw_size.assert_not_called()

    def test_utf8_character_crossing_old_sample_boundary_is_text(self):
        old_data = b"a" * 8191 + "中\n".encode("utf-8")
        new_data = b"a" * 8191 + "文\n".encode("utf-8")

        result = DiffEngine(_BytesVCS(old_data, new_data)).generate_diff(
            "old", "new"
        )

        self.assertIn('<table class="diff"', result.files[0].side_by_side_html)
        self.assertTrue(result.files[0].line_counts_complete)

    def test_gb18030_character_crossing_old_sample_boundary_is_text(self):
        data = b"a" * 8191 + "中文\n".encode("gb18030")

        self.assertFalse(DiffEngine._content_is_binary(data))

    def test_common_ansi_escape_sequences_remain_text(self):
        colored_log = b"\x1b[31mERROR\x1b[0m normal text\n" * 100

        self.assertFalse(DiffEngine._content_is_binary(colored_log))

    def test_terminal_backspace_progress_log_remains_text(self):
        old_data = b"0%\b\b1%\b\b2%\r\n"
        new_data = b"0%\b\b1%\b\b3%\r\n"

        result = DiffEngine(_BytesVCS(old_data, new_data, "progress.log")).generate_diff(
            "old", "new"
        )

        self.assertIn('<table class="diff"', result.files[0].side_by_side_html)
        self.assertTrue(result.files[0].line_counts_complete)

    def test_bomless_utf16_text_keeps_line_detail(self):
        old_data = "old\n".encode("utf-16-le")
        new_data = "new\n".encode("utf-16-le")

        result = DiffEngine(_BytesVCS(old_data, new_data)).generate_diff(
            "old", "new"
        )

        self.assertEqual("old\n", result.files[0].old_content)
        self.assertEqual("new\n", result.files[0].new_content)
        self.assertIn('<table class="diff"', result.files[0].side_by_side_html)
        self.assertTrue(result.files[0].line_counts_complete)

    def test_bomless_utf16_chinese_text_keeps_line_detail(self):
        old_text = "中文源码第一行\n中文源码第二行\n"
        new_text = "中文源码第一行\n中文源码已修改\n"
        for codec in ("utf-16-le", "utf-16-be"):
            with self.subTest(codec=codec):
                result = DiffEngine(_BytesVCS(
                    old_text.encode(codec), new_text.encode(codec)
                )).generate_diff("old", "new")

                self.assertEqual(old_text, result.files[0].old_content)
                self.assertEqual(new_text, result.files[0].new_content)
                self.assertIn(
                    '<table class="diff"', result.files[0].side_by_side_html
                )
                self.assertTrue(result.files[0].line_counts_complete)

    def test_bomless_utf32_text_keeps_line_detail(self):
        for codec in ("utf-32-le", "utf-32-be"):
            with self.subTest(codec=codec):
                old_data = "old\n".encode(codec)
                new_data = "new\n".encode(codec)

                result = DiffEngine(_BytesVCS(old_data, new_data)).generate_diff(
                    "old", "new"
                )

                self.assertEqual("old\n", result.files[0].old_content)
                self.assertEqual("new\n", result.files[0].new_content)
                self.assertIn(
                    '<table class="diff"', result.files[0].side_by_side_html
                )

    def test_strictly_decoded_nul_is_still_binary(self):
        for data in (
            b"\xef\xbb\xbftext\x00value",
            b"\xff\xfet\x00e\x00x\x00t\x00\x00\x00",
        ):
            with self.subTest(data=data):
                self.assertTrue(DiffEngine._content_is_binary(data))

    def test_null_content_remains_binary(self):
        old_data = b"text\x00old"
        new_data = b"text\x00new"

        self.assertTrue(DiffEngine._content_is_binary(old_data))
        result = DiffEngine(_BytesVCS(old_data, new_data)).generate_diff(
            "old", "new"
        )
        self.assertIn("二进制归档文件", result.files[0].side_by_side_html)
        self.assertFalse(result.files[0].line_counts_complete)

    def test_unknown_legacy_encoding_keeps_pre_august_27_text_detail(self):
        old_data = b"He said \x93hello\x94\r\n"
        new_data = b"He said \x93world\x94\r\n"

        self.assertFalse(DiffEngine._content_is_binary(old_data))
        result = DiffEngine(_BytesVCS(old_data, new_data)).generate_diff(
            "old", "new"
        )
        self.assertIn('<table class="diff"', result.files[0].side_by_side_html)
        self.assertTrue(result.files[0].line_counts_complete)

    def test_single_undecodable_bytes_keep_old_text_fallback(self):
        result = DiffEngine(_BytesVCS(b"\x81", b"\x82")).generate_diff(
            "old", "new"
        )

        self.assertIn('<table class="diff"', result.files[0].side_by_side_html)
        self.assertTrue(result.files[0].line_counts_complete)


class LineCountCompatibilityTests(unittest.TestCase):
    def test_shifted_repeated_middle_uses_exact_lcs_counts(self):
        old_lines = ["old-start"] + ["repeat"] * 100 + ["old-end"]
        new_lines = ["new-start", "extra"] + ["repeat"] * 100 + ["new-end"]

        self.assertEqual(
            (3, 2), DiffEngine._count_line_changes(old_lines, new_lines)
        )

    def test_repeated_common_edges_use_exact_minimal_line_changes(self):
        self.assertEqual(
            (1, 0),
            DiffEngine._count_line_changes(
                ["a", "a", "a"], ["a", "b", "a", "a"]
            ),
        )

    def test_repeated_interleaving_uses_exact_lcs(self):
        self.assertEqual(
            (1, 1),
            DiffEngine._count_line_changes(
                ["A", "B", "A"], ["B", "C", "A"]
            ),
        )

    def test_repeated_lines_do_not_distort_one_line_change(self):
        old_lines = ["repeat"] * 300 + ["old"] + ["repeat"] * 300
        new_lines = ["repeat"] * 300 + ["new"] + ["repeat"] * 300

        self.assertEqual(
            (1, 1), DiffEngine._count_line_changes(old_lines, new_lines)
        )

    def test_dense_repeated_lines_do_not_expand_matching_cartesian_product(self):
        old_lines = ["old-start"] + ["repeat"] * 10_000 + ["old-end"]
        new_lines = ["new-start"] + ["repeat"] * 10_000 + ["new-end"]

        self.assertEqual(
            (2, 2), DiffEngine._count_line_changes(old_lines, new_lines)
        )

    def test_lcs_matches_dynamic_programming_oracle(self):
        rng = random.Random(20260830)

        def oracle(old, new):
            row = [0] * (len(new) + 1)
            for old_value in old:
                previous = 0
                for index, new_value in enumerate(new, 1):
                    saved = row[index]
                    if old_value == new_value:
                        row[index] = previous + 1
                    else:
                        row[index] = max(row[index], row[index - 1])
                    previous = saved
            return row[-1]

        alphabet = ["A", "B", "C", "D"]
        for _ in range(500):
            old = [rng.choice(alphabet) for _ in range(rng.randrange(10))]
            new = [rng.choice(alphabet) for _ in range(rng.randrange(10))]
            self.assertEqual(oracle(old, new), DiffEngine._lcs_length(old, new))


class DisabledReportBudgetCompatibilityTests(unittest.TestCase):
    def test_disabled_report_budget_skips_unused_size_accounting(self):
        class NoLength(bytes):
            def __len__(self):
                raise AssertionError("disabled report budget must not count bytes")

        engine = DiffEngine(_BytesVCS(b"old", b"new"))

        self.assertIsNone(engine._report_budget)
        self.assertEqual(
            "", engine._reserve_report_budget([NoLength(b"payload")], 4, 4)
        )


class ReportManifestCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _result():
        result = DiffEngine(_BytesVCS(b"old\n", b"new\n")).generate_diff(
            "old", "new"
        )
        result.project_name = "demo"
        return result

    def test_single_report_reuses_complete_file_data_for_manifest(self):
        with project_temp_dir() as root:
            output = os.path.join(root, "report.html")
            ReportGenerator().generate(self._result(), output)
            with open(output, encoding="utf-8") as stream:
                rendered = stream.read()

        self.assertIn("var manifestData = fileData;", rendered)
        self.assertNotIn("manifestData.push({", rendered)

    def test_multi_report_reuses_complete_file_data_for_manifest(self):
        result = self._result()
        projects = [{
            "project_name": "demo",
            "vcs_type": "FakeVCS",
            "show_project_root": True,
            "diff_result": result,
        }]
        with project_temp_dir() as root:
            output = os.path.join(root, "multi.html")
            ReportGenerator().generate_multi(projects, output)
            with open(output, encoding="utf-8") as stream:
                rendered = stream.read()

        self.assertIn("var manifestData = fileData;", rendered)
        self.assertNotIn("manifestData.push({", rendered)


class LegacyBaseVCSCompatibilityTests(unittest.TestCase):
    def test_empty_content_is_a_successful_empty_byte_endpoint(self):
        vcs = _LegacyVCS("")

        self.assertEqual(b"", vcs.get_file_content_bytes("v", "empty.txt"))
        with project_temp_dir() as root:
            target = os.path.join(root, "empty.txt")
            vcs.export_file_to_path("v", "empty.txt", target)
            with open(target, "rb") as stream:
                self.assertEqual(b"", stream.read())

    def test_legacy_export_does_not_query_or_reject_by_estimated_size(self):
        class NoSizeQuery(_LegacyVCS):
            def get_file_size(self, version, file_path):
                raise AssertionError("default legacy export must not preflight size")

        vcs = NoSizeQuery("complete payload")
        with project_temp_dir() as root:
            target = os.path.join(root, "large.txt")
            vcs.export_file_to_path("v", "large.txt", target)
            with open(target, "rb") as stream:
                self.assertEqual(b"complete payload", stream.read())

    def test_missing_content_still_fails_instead_of_becoming_empty(self):
        vcs = _LegacyVCS(None)

        self.assertIsNone(vcs.get_file_content_bytes("v", "missing.txt"))
        with project_temp_dir() as root:
            target = os.path.join(root, "missing.txt")
            with self.assertRaisesRegex(RuntimeError, "无法读取版本"):
                vcs.export_file_to_path("v", "missing.txt", target)
            self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
