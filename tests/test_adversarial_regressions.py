import io
import os
import subprocess
import tarfile
import tempfile
import unittest
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
            with self.assertRaisesRegex(ValueError, "越界路径"):
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
    def test_git_type_change_is_reported_as_modified(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = []
        vcs._run = lambda args: "T\tchanged.txt\n"

        files = vcs.get_changed_files("old", "new")

        self.assertEqual(1, len(files))
        self.assertEqual(ChangeType.MODIFIED, files[0].change_type)

    def test_unknown_git_change_type_fails_instead_of_being_omitted(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = []
        vcs._run = lambda args: "X\tmystery.txt\n"

        with self.assertRaisesRegex(RuntimeError, "暂不支持"):
            vcs.get_changed_files("old", "new")

    def test_svn_xml_skips_directories_and_treats_replaced_file_as_modified(self):
        class FakeSVN(SVNVCS):
            def __init__(self, project_path):
                self.project_path = project_path
                self.exclude_patterns = []

            def _run(self, args):
                return """<?xml version="1.0"?>
<diff><paths>
  <path item="deleted" props="none" kind="dir">gone-dir</path>
  <path item="replaced" props="none" kind="file">same-name.txt</path>
</paths></diff>"""

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
            stdout=b"native",
            stderr=b"",
        )

        with mock.patch("vcs.svn_vcs.subprocess.run", return_value=completed) as run:
            data = vcs.get_file_content_bytes("r12", "deleted.txt")

        self.assertEqual(b"line\r\n", data)
        args = run.call_args.args[0]
        self.assertIn("-r", args)
        self.assertIn("12", args)
        self.assertIn("https://example.invalid/repo/project/deleted.txt@12", args)


class ReportAndTaskSafetyTests(unittest.TestCase):
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

    def test_failed_config_serialization_preserves_previous_file(self):
        with project_temp_dir() as root:
            config_path = os.path.join(root, "compareTool_config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write('{"kept": true}')

            with mock.patch.object(main, "CONFIG_FILE", config_path), \
                    mock.patch.object(main, "CONFIG_DIR", root), \
                    mock.patch("main.json.dump", side_effect=OSError("write failed")):
                main._save_config({"new": "value"})
                self.assertEqual({"kept": True}, main._load_config())


if __name__ == "__main__":
    unittest.main()
