import io
import os
import subprocess
import stat
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from unittest import mock

import main
from diff_engine import DiffEngine, DiffResult, FileDiff
from file_exporter import FileExporter
from main import CompareToolApp
from report_generator import ReportGenerator
from vcs.archive_vcs import ArchiveVCS
from vcs.base import ChangedFile, ChangeType
from vcs.git_vcs import GitVCS
from vcs.svn_vcs import SVNVCS


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


class VersionedFakeVCS:
    def __init__(self, project_path, changed_files, contents, working="WORKTREE"):
        self.project_path = project_path
        self.changed_files = changed_files
        self.contents = contents
        self.working = working

    def get_changed_files(self, old_version, new_version):
        return list(self.changed_files)

    def get_file_content(self, version, file_path):
        return self.contents.get((version, file_path), b"").decode("utf-8")

    def get_file_content_working(self, file_path):
        return self.working

    def get_file_content_bytes(self, version, file_path):
        return self.contents.get((version, file_path))

    def get_file_content_raw_bytes(self, version, file_path):
        return self.contents.get((version, file_path))


class SelectedVersionDiffTests(unittest.TestCase):
    def test_added_file_uses_selected_new_version_even_if_worktree_is_directory(self):
        with project_temp_dir() as root:
            os.makedirs(os.path.join(root, "added.txt"))
            vcs = VersionedFakeVCS(
                root,
                [ChangedFile("added.txt", ChangeType.ADDED)],
                {("new", "added.txt"): b"SELECTED_NEW"},
            )

            result = DiffEngine(vcs).generate_diff("old", "new")

            self.assertEqual(1, len(result.files))
            self.assertEqual("SELECTED_NEW", result.files[0].new_content)
            self.assertNotIn("WORKTREE", result.files[0].side_by_side_html)

    def test_renamed_file_uses_selected_version_and_counts_replaced_lines(self):
        with project_temp_dir() as root:
            vcs = VersionedFakeVCS(
                root,
                [ChangedFile("new.txt", ChangeType.RENAMED, old_path="old.txt")],
                {
                    ("old", "old.txt"): b"before\n",
                    ("new", "new.txt"): b"after\n",
                },
            )

            file_diff = DiffEngine(vcs).generate_diff("old", "new").files[0]

            self.assertEqual("after\n", file_diff.new_content)
            self.assertEqual(1, file_diff.added_lines)
            self.assertEqual(1, file_diff.deleted_lines)

    def test_rename_detection_uses_raw_bytes_not_export_normalization(self):
        class NormalizingVCS(VersionedFakeVCS):
            def get_file_content_bytes(self, version, file_path):
                return b"same\r\n"

        with project_temp_dir() as root:
            vcs = NormalizingVCS(
                root,
                [
                    ChangedFile("old.txt", ChangeType.DELETED),
                    ChangedFile("new.txt", ChangeType.ADDED),
                ],
                {
                    ("old", "old.txt"): b"same\r\n",
                    ("new", "new.txt"): b"same\n",
                },
            )

            result = DiffEngine(vcs).generate_diff("old", "new")

            self.assertEqual(
                [ChangeType.DELETED, ChangeType.ADDED],
                [item.change_type for item in result.files],
            )

    def test_extensionless_binary_is_not_sent_to_html_diff(self):
        with project_temp_dir() as root:
            vcs = VersionedFakeVCS(
                root,
                [ChangedFile("payload", ChangeType.MODIFIED)],
                {
                    ("old", "payload"): b"old\x00binary",
                    ("new", "payload"): b"new\x00binary",
                },
            )

            file_diff = DiffEngine(vcs).generate_diff("old", "new").files[0]

        self.assertIn("无扩展名 二进制归档文件", file_diff.side_by_side_html)
        self.assertEqual("", file_diff.old_content)
        self.assertEqual("", file_diff.new_content)

    def test_large_text_skips_quadratic_html_diff_but_stays_changed(self):
        with project_temp_dir() as root:
            vcs = VersionedFakeVCS(
                root,
                [ChangedFile("large.txt", ChangeType.MODIFIED)],
                {
                    ("old", "large.txt"): b"12345",
                    ("new", "large.txt"): b"12346",
                },
            )
            with mock.patch.object(DiffEngine, "MAX_TEXT_DIFF_BYTES", 4):
                file_diff = DiffEngine(vcs).generate_diff("old", "new").files[0]

        self.assertIn("文件过大，已跳过逐行差异展示", file_diff.side_by_side_html)
        self.assertEqual(ChangeType.MODIFIED, file_diff.change_type)

    def test_metadata_only_change_is_visible_even_when_bytes_match(self):
        with project_temp_dir() as root:
            vcs = VersionedFakeVCS(
                root,
                [ChangedFile(
                    "script.sh",
                    ChangeType.MODIFIED,
                    metadata_changes=["Git 文件模式：100644 → 100755"],
                    old_executable=False,
                    new_executable=True,
                )],
                {
                    ("old", "script.sh"): b"echo ok\n",
                    ("new", "script.sh"): b"echo ok\n",
                },
            )

            file_diff = DiffEngine(vcs).generate_diff("old", "new").files[0]

        self.assertIn("文件元数据变化", file_diff.side_by_side_html)
        self.assertIn("100644 → 100755", file_diff.side_by_side_html)


class ArchiveBoundaryTests(unittest.TestCase):
    def test_zip_parent_traversal_is_rejected_without_writing_outside(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "bad.zip")
            dest = os.path.join(root, "dest")
            outside = os.path.join(root, "escaped.txt")
            os.makedirs(dest)
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escaped.txt", b"owned")

            instance = ArchiveVCS.__new__(ArchiveVCS)
            with self.assertRaisesRegex(ValueError, "不安全路径"):
                instance._extract_zip(archive, dest)

            self.assertFalse(os.path.exists(outside))

    def test_tar_links_are_rejected(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "bad.tar")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with tarfile.open(archive, "w") as tf:
                regular = tarfile.TarInfo("safe.txt")
                regular.size = 4
                tf.addfile(regular, io.BytesIO(b"safe"))
                link = tarfile.TarInfo("escape-link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../outside"
                tf.addfile(link)

            instance = ArchiveVCS.__new__(ArchiveVCS)
            with self.assertRaisesRegex(ValueError, "链接/特殊文件"):
                instance._extract_tar(archive, dest)

            self.assertFalse(os.path.exists(os.path.join(dest, "safe.txt")))

    def test_zip_duplicate_file_target_is_rejected_before_writing(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "duplicate.zip")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr("same.txt", b"first")
                    zf.writestr("same.txt", b"second")

            instance = ArchiveVCS.__new__(ArchiveVCS)
            with self.assertRaisesRegex(ValueError, "同一 Windows 路径"):
                instance._extract_zip(archive, dest)

            self.assertEqual([], os.listdir(dest))

    def test_zip_case_collision_is_rejected_before_writing(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "case.zip")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("Demo.txt", b"first")
                zf.writestr("demo.txt", b"second")

            instance = ArchiveVCS.__new__(ArchiveVCS)
            with self.assertRaisesRegex(ValueError, "同一 Windows 路径"):
                instance._extract_zip(archive, dest)

            self.assertEqual([], os.listdir(dest))

    def test_zip_link_is_rejected_before_earlier_regular_member_is_written(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "link.zip")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("safe.txt", b"safe")
                zf.writestr(link, b"../outside")

            instance = ArchiveVCS.__new__(ArchiveVCS)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                instance._extract_zip(archive, dest)

            self.assertEqual([], os.listdir(dest))


class ExportIntegrityTests(unittest.TestCase):
    @staticmethod
    def _result(files):
        return DiffResult(
            project_path="demo",
            project_name="demo",
            vcs_type="Fake",
            old_version="old",
            new_version="new",
            files=files,
        )

    def test_read_failure_keeps_previous_exports_and_raises(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            os.makedirs(old_dir)
            os.makedirs(new_dir)
            with open(os.path.join(old_dir, "keep.txt"), "w", encoding="utf-8") as f:
                f.write("old")
            with open(os.path.join(new_dir, "keep.txt"), "w", encoding="utf-8") as f:
                f.write("new")

            result = self._result([FileDiff("missing.bin", ChangeType.ADDED)])
            vcs = type("MissingVCS", (), {
                "get_file_content_bytes": lambda self, version, path: None,
            })()

            with self.assertRaisesRegex(RuntimeError, "已中止导出"):
                FileExporter(result, vcs).export(old_dir, new_dir)

            self.assertTrue(os.path.isfile(os.path.join(old_dir, "keep.txt")))
            self.assertTrue(os.path.isfile(os.path.join(new_dir, "keep.txt")))
            self.assertFalse(os.path.exists(os.path.join(new_dir, "missing.bin")))

    def test_relative_path_escape_is_rejected(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            result = self._result([FileDiff("../escaped.txt", ChangeType.ADDED)])
            vcs = type("BytesVCS", (), {
                "get_file_content_bytes": lambda self, version, path: b"owned",
            })()

            with self.assertRaisesRegex(RuntimeError, "路径不安全"):
                FileExporter(result, vcs).export(old_dir, new_dir)

            self.assertFalse(os.path.exists(os.path.join(root, "escaped.txt")))

    def test_case_only_file_name_collision_fails_instead_of_overwriting(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            result = self._result([
                FileDiff("Demo.txt", ChangeType.ADDED),
                FileDiff("demo.txt", ChangeType.ADDED),
            ])
            vcs = type("BytesVCS", (), {
                "get_file_content_bytes": lambda self, version, path: path.encode("utf-8"),
            })()

            with self.assertRaisesRegex(RuntimeError, "Windows 路径冲突"):
                FileExporter(result, vcs).export(old_dir, new_dir)

            self.assertFalse(os.path.exists(new_dir))

    def test_success_replaces_both_export_directories(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            os.makedirs(old_dir)
            os.makedirs(new_dir)
            for directory in (old_dir, new_dir):
                with open(os.path.join(directory, "stale.txt"), "w", encoding="utf-8") as f:
                    f.write("stale")

            result = self._result([FileDiff("changed.txt", ChangeType.MODIFIED)])
            vcs = type("BytesVCS", (), {
                "get_file_content_bytes": lambda self, version, path: version.encode("ascii"),
            })()

            FileExporter(result, vcs).export(old_dir, new_dir)

            self.assertFalse(os.path.exists(os.path.join(old_dir, "stale.txt")))
            self.assertFalse(os.path.exists(os.path.join(new_dir, "stale.txt")))
            with open(os.path.join(old_dir, "changed.txt"), "rb") as f:
                self.assertEqual(b"old", f.read())
            with open(os.path.join(new_dir, "changed.txt"), "rb") as f:
                self.assertEqual(b"new", f.read())

    def test_second_install_failure_rolls_back_both_directories(self):
        with project_temp_dir() as root:
            old_target = os.path.join(root, "old")
            new_target = os.path.join(root, "new")
            old_stage = os.path.join(root, "old-stage")
            new_stage = os.path.join(root, "new-stage")
            for path, content in (
                (old_target, "old-original"),
                (new_target, "new-original"),
                (old_stage, "old-staged"),
                (new_stage, "new-staged"),
            ):
                os.makedirs(path)
                with open(os.path.join(path, "value.txt"), "w", encoding="utf-8") as f:
                    f.write(content)

            real_replace = os.replace
            replace_count = 0

            def fail_fourth_replace(src, dst):
                nonlocal replace_count
                replace_count += 1
                if replace_count == 4:
                    raise OSError("simulated second install failure")
                return real_replace(src, dst)

            with mock.patch("file_exporter.os.replace", side_effect=fail_fourth_replace):
                with self.assertRaisesRegex(OSError, "simulated"):
                    FileExporter._replace_outputs([
                        (old_stage, old_target),
                        (new_stage, new_target),
                    ])

            for target, expected in (
                (old_target, "old-original"),
                (new_target, "new-original"),
            ):
                with open(os.path.join(target, "value.txt"), encoding="utf-8") as f:
                    self.assertEqual(expected, f.read())


class VCSParsingTests(unittest.TestCase):
    def test_git_mode_only_change_is_preserved_as_metadata(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = []
        vcs._version_pins = {"old": "a" * 40, "new": "b" * 40}
        vcs._run_bytes = lambda args: (
            b":100644 100755 aaaaaaa aaaaaaa M\x00script.sh\x00"
        )

        files = vcs.get_changed_files("old", "new")

        self.assertEqual(1, len(files))
        self.assertEqual(ChangeType.MODIFIED, files[0].change_type)
        self.assertEqual(["Git 文件模式：100644 → 100755"], files[0].metadata_changes)
        self.assertFalse(files[0].old_executable)
        self.assertTrue(files[0].new_executable)

    def test_git_type_change_fails_closed(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = []
        vcs._version_pins = {"old": "a" * 40, "new": "b" * 40}
        vcs._run_bytes = lambda args: (
            b":100644 120000 aaaaaaa bbbbbbb T\x00changed.txt\x00"
        )

        with self.assertRaisesRegex(RuntimeError, "文件类型发生变化"):
            vcs.get_changed_files("old", "new")

    def test_unknown_git_change_type_fails_instead_of_being_omitted(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = []
        vcs._version_pins = {"old": "a" * 40, "new": "b" * 40}
        vcs._run_bytes = lambda args: (
            b":100644 100644 aaaaaaa bbbbbbb X\x00mystery.txt\x00"
        )

        with self.assertRaisesRegex(RuntimeError, "暂不支持"):
            vcs.get_changed_files("old", "new")

    def test_svn_xml_skips_directories_and_treats_replaced_file_as_modified(self):
        class FakeSVN(SVNVCS):
            def __init__(self, project_path):
                self.project_path = project_path
                self.exclude_patterns = []

            def _pin_source_identity(self):
                self._source_identity_pinned = True
                self._pinned_project_url = "https://example.invalid/repo/project"
                self._pinned_peg_revision = "2"

            def _run(self, args):
                return """<?xml version="1.0"?>
<diff><paths>
  <path item="deleted" props="none" kind="dir">gone-dir</path>
  <path item="replaced" props="none" kind="file">same-name.txt</path>
</paths></diff>"""

            def _compare_endpoint_metadata(self, *args):
                return {}

            def _get_node_kind(self, *args):
                return "file"

            def _get_properties(self, *args):
                return {}

        with project_temp_dir() as root:
            files = FakeSVN(root).get_changed_files("1", "2")

        self.assertEqual(1, len(files))
        self.assertEqual("same-name.txt", files[0].path)
        self.assertEqual(ChangeType.MODIFIED, files[0].change_type)

    def test_svn_eol_style_is_loaded_from_selected_revision_url(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs.project_path = "demo"
        vcs._svn = "svn"
        vcs._cached_repo_url = "https://example.invalid/repo/project"
        vcs._eol_cache = {}
        vcs.get_file_content_raw_bytes = lambda version, path: b"line\n"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                b'<?xml version="1.0"?><properties><target path="x">'
                b'<property name="svn:eol-style">native</property>'
                b'</target></properties>'
            ),
            stderr=b"",
        )

        with mock.patch("vcs.svn_vcs.subprocess.run", return_value=completed) as run:
            data = vcs.get_file_content_bytes("r12", "deleted.txt")

        self.assertEqual(b"line\r\n", data)
        args = run.call_args.args[0]
        self.assertIn("-r", args)
        self.assertIn("12", args)
        self.assertIn("https://example.invalid/repo/project/deleted.txt@12", args)

    def test_svn_property_lookup_failure_fails_closed(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs.project_path = "demo"
        vcs._svn = "svn"
        vcs._cached_repo_url = "https://example.invalid/repo/project"
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"network failed"
        )

        with mock.patch("vcs.svn_vcs.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "无法读取 SVN 文件属性"):
                vcs._get_eol_style("r12", "demo.txt")

    def test_svn_unsupported_property_change_fails_closed(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._get_properties = mock.Mock(side_effect=[
            {"custom:deployment": "old"},
            {"custom:deployment": "new"},
        ])

        with self.assertRaisesRegex(RuntimeError, "custom:deployment"):
            vcs._compare_endpoint_metadata("1", "demo.txt", "2", "demo.txt")


class ReportAndTaskSafetyTests(unittest.TestCase):
    def test_source_and_output_directories_must_not_overlap(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "source")
            os.makedirs(source)

            with self.assertRaisesRegex(ValueError, "重叠"):
                CompareToolApp._validate_source_output_separation(
                    [source], [os.path.join(source, "generated")]
                )
            with self.assertRaisesRegex(ValueError, "重叠"):
                CompareToolApp._validate_source_output_separation(
                    [source], [root]
                )
            with self.assertRaisesRegex(ValueError, "输出文件与输入源码路径重叠"):
                CompareToolApp._validate_source_output_separation(
                    [source], [], [os.path.join(source, "report.html")]
                )
            source_below_file_target = os.path.join(root, "report.html", "source")
            os.makedirs(source_below_file_target)
            with self.assertRaisesRegex(ValueError, "输出文件与输入源码路径重叠"):
                CompareToolApp._validate_source_output_separation(
                    [source_below_file_target], [], [os.path.join(root, "report.html")]
                )

            CompareToolApp._validate_source_output_separation(
                [source], [os.path.join(root, "sibling-output")]
            )

    def test_multi_run_uses_self_contained_unique_subdirectory(self):
        with project_temp_dir() as root:
            app = CompareToolApp.__new__(CompareToolApp)
            app.output_dir_var = mock.Mock()
            app.output_dir_var.get.return_value = root
            app.output_batch_var = mock.Mock()
            app.output_batch_var.get.return_value = "20260827"

            run_dir, report, old_export, new_export = app._multi_run_paths()

            batch_dir = os.path.normpath(os.path.join(root, "20260827"))
            self.assertEqual(batch_dir, os.path.dirname(os.path.normpath(run_dir)))
            self.assertTrue(os.path.basename(run_dir).startswith("multi_run_"))
            self.assertEqual(os.path.normpath(run_dir), os.path.dirname(os.path.normpath(report)))
            self.assertEqual(os.path.normpath(os.path.join(run_dir, "oldVersion")), os.path.normpath(old_export))
            self.assertEqual(os.path.normpath(os.path.join(run_dir, "newVersion")), os.path.normpath(new_export))

    def test_reserved_windows_names_are_sanitized_consistently(self):
        self.assertEqual("_CON", CompareToolApp._sanitize_project_name("CON"))
        self.assertEqual("_lpt1.txt", CompareToolApp._sanitize_project_name("lpt1.txt"))
        self.assertEqual("_NUL", CompareToolApp._sanitize_output_batch_name("NUL"))
        self.assertEqual("demo_name", CompareToolApp._sanitize_project_name("demo:name"))

    def test_diff_table_escapes_repository_file_names(self):
        with project_temp_dir() as root:
            path = '<img src=x onerror="alert(1)">.txt'
            vcs = VersionedFakeVCS(
                root,
                [ChangedFile(path, ChangeType.ADDED)],
                {("new", path): b"content"},
            )

            rendered = DiffEngine(vcs).generate_diff("old", "new").files[0].side_by_side_html

        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;.txt", rendered)
        self.assertNotIn('<img src=x onerror="alert(1)">', rendered)

    def test_report_metadata_is_html_escaped_but_generated_diff_html_is_preserved(self):
        result = DiffResult(
            project_path="demo",
            project_name="demo",
            vcs_type="Fake",
            old_version='<img src=x onerror="alert(1)">',
            new_version="new",
            files=[
                FileDiff(
                    "demo.txt",
                    ChangeType.MODIFIED,
                    side_by_side_html="<div>trusted diff</div>",
                )
            ],
        )
        generator = ReportGenerator()
        rendered = generator.env.get_template("report.html").render(
            project_name=result.project_name,
            project_path=result.project_path,
            vcs_type=result.vcs_type,
            old_version=result.old_version,
            new_version=result.new_version,
            summary=result.summary,
            files=result.files,
            show_project_root=True,
            generated_at="2026-07-30 00:00:00",
        )

        self.assertIn("&lt;img src=x onerror=&#34;alert(1)&#34;&gt;", rendered)
        self.assertNotIn('<span>demo &nbsp;|&nbsp; Fake &nbsp;|&nbsp;\n        旧版本: <img', rendered)
        self.assertIn("<div>trusted diff</div>", rendered)

    def test_multi_version_report_uses_selected_versions_and_result_labels(self):
        result = DiffResult(
            project_path="demo",
            project_name="demo",
            vcs_type="GitMultiVersionVCS",
            old_version="r3, r6",
            new_version="文件级首尾端点",
            files=[],
        )
        generator = ReportGenerator()
        rendered = generator.env.get_template("report.html").render(
            project_name=result.project_name,
            project_path=result.project_path,
            vcs_type=result.vcs_type,
            old_version=result.old_version,
            new_version=result.new_version,
            summary=result.summary,
            files=result.files,
            show_project_root=True,
            generated_at="2026-08-24 00:00:00",
        )

        self.assertIn("选中版本: r3, r6", rendered)
        self.assertIn("生成结果: 文件级首尾端点", rendered)
        self.assertNotIn("旧版本: r3, r6", rendered)
        self.assertIn(
            "var versionSummary = '选中版本: ' + oldVersion + ' | 生成结果: ' + newVersion;",
            rendered,
        )

    def test_multi_version_project_change_restores_readonly_result_label(self):
        app = CompareToolApp.__new__(CompareToolApp)
        app.dir_entry = mock.Mock()
        app.dir_entry.get.return_value = os.path.join("D:\\", "new-project")
        app._last_project_path = app._normalize_project_path(
            os.path.join("D:\\", "old-project")
        )
        app._version_request_id = 7
        app._project_name_manual = True
        app.vcs_var = mock.Mock()
        app.vcs_var.get.return_value = "git_multi"
        app.old_version_var = mock.Mock()
        app.new_version_var = mock.Mock()
        app._reset_version_list_state = mock.Mock()
        app.version_listbox = mock.Mock()
        app.fill_btn_frame = mock.Mock()
        app.status_var = mock.Mock()
        app._refresh_project_name_default = mock.Mock()
        app._switch_exclude_rules_for_current_source = mock.Mock()
        app._update_output_paths = mock.Mock()

        app._on_project_path_changed()

        app.old_version_var.set.assert_called_once_with("")
        app.new_version_var.set.assert_called_once_with("文件级首尾端点")

    def test_project_names_are_compared_case_insensitively(self):
        app = CompareToolApp.__new__(CompareToolApp)
        app._multi_tasks = [{"project_name": "Demo"}]

        with mock.patch("main.messagebox.showwarning") as warning:
            unique = app._ensure_unique_task_name("demo")

        self.assertFalse(unique)
        warning.assert_called_once()

    def test_window_close_is_blocked_while_generation_is_running(self):
        app = CompareToolApp.__new__(CompareToolApp)
        app._generating = True
        app.root = mock.Mock()
        app._save_current_config = mock.Mock()

        with mock.patch("main.messagebox.showwarning") as warning:
            app._on_close()

        warning.assert_called_once()
        app._save_current_config.assert_not_called()
        app.root.destroy.assert_not_called()


class ConfigPersistenceTests(unittest.TestCase):
    def test_config_save_replaces_file_and_load_rejects_non_object_json(self):
        with project_temp_dir() as root:
            config_path = os.path.join(root, "compareTool_config.json")
            with mock.patch.object(main, "CONFIG_FILE", config_path), \
                    mock.patch.object(main, "CONFIG_DIR", root):
                main._save_config({"multi_tasks": [{"project_name": "demo"}]})
                self.assertEqual(
                    {"multi_tasks": [{"project_name": "demo"}]},
                    main._load_config(),
                )

                with open(config_path, "w", encoding="utf-8") as f:
                    f.write("[]")
                self.assertEqual({}, main._load_config())
                with self.assertRaisesRegex(main.ConfigSaveError, "避免覆盖"):
                    main._save_config({"would": "overwrite"})
                with open(config_path, encoding="utf-8") as f:
                    self.assertEqual("[]", f.read())

    def test_failed_config_serialization_preserves_previous_file(self):
        with project_temp_dir() as root:
            config_path = os.path.join(root, "compareTool_config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('{"kept": true}')

            with mock.patch.object(main, "CONFIG_FILE", config_path), \
                    mock.patch.object(main, "CONFIG_DIR", root), \
                    mock.patch("main.json.dump", side_effect=OSError("write failed")):
                with self.assertRaises(main.ConfigSaveError):
                    main._save_config({"new": "value"})
                self.assertEqual({"kept": True}, main._load_config())


if __name__ == "__main__":
    unittest.main()
