import io
import os
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from delivery_instructions import write_delivery_instructions
from diff_engine import DiffEngine
from file_exporter import FileExporter
from main import CompareToolApp
from report_generator import ReportGenerator
from vcs.archive_vcs import ArchiveVCS


def write_archive(path, entries, mode=0o644):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in entries.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                if name.endswith("/"):
                    info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                archive.writestr(info, payload)
    else:
        with tarfile.open(path, "w") as archive:
            for name, payload in entries.items():
                info = tarfile.TarInfo(name)
                info.mode = mode
                info.size = len(payload)
                if name.endswith("/"):
                    info.type = tarfile.DIRTYPE
                    info.size = 0
                archive.addfile(info, io.BytesIO(payload))


class ArchiveComparisonRootTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(".tmp", exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.root = Path(self.temp.name).resolve()
        self.env = mock.patch.dict(os.environ, {
            "COMPARETOOL_TEMP_DIR": str(self.root / "runtime"),
            "COMPARETOOL_TRANSACTION_KEY_FILE": str(self.root / "transaction.key"),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def vcs(self, old, new, suffix=".zip", **kwargs):
        old_path, new_path = self.root / ("old" + suffix), self.root / ("new" + suffix)
        write_archive(old_path, old)
        write_archive(new_path, new)
        vcs = ArchiveVCS(str(old_path), str(new_path), **kwargs)
        self.addCleanup(vcs.cleanup)
        return vcs

    def test_default_keeps_original_archive_paths(self):
        vcs = self.vcs({"old/value.txt": b"old"}, {"new/value.txt": b"new"})
        self.assertEqual({"old/value.txt", "new/value.txt"}, {f.path for f in vcs.get_changed_files()})
        self.assertEqual("", vcs.comparison_note)

    def test_zip_and_tar_rebase_report_exports_and_renames_together(self):
        for suffix in (".zip", ".tar"):
            with self.subTest(suffix=suffix):
                vcs = self.vcs({
                    "old&release/config/value.txt": b"old\n",
                    "old&release/same.txt": b"same",
                    "old&release/rename-old.txt": b"rename",
                    "old&release/delete.txt": b"deleted",
                }, {
                    "new&release/config/value.txt": b"new\n",
                    "new&release/same.txt": b"same",
                    "new&release/rename-new.txt": b"rename",
                    "new&release/add.bin": b"\0added",
                }, suffix, ignore_single_root=True)
                old, new = vcs.old_archive, vcs.new_archive
                result = DiffEngine(vcs).generate_diff(old, new)
                result.project_name = "Demo"
                self.assertEqual({
                    "config/value.txt": "M", "rename-new.txt": "R", "delete.txt": "D", "add.bin": "A",
                }, {f.file_path: f.report_type for f in result.files})
                output = self.root / suffix[1:]
                output.mkdir()
                FileExporter(result, vcs).export(str(output / "oldVersion"), str(output / "newVersion"), "Demo")
                self.assertEqual(b"old\n", (output / "oldVersion/Demo/config/value.txt").read_bytes())
                self.assertEqual(b"new\n", (output / "newVersion/Demo/config/value.txt").read_bytes())
                self.assertTrue((output / "oldVersion/Demo/rename-old.txt").is_file())
                self.assertTrue((output / "newVersion/Demo/rename-new.txt").is_file())
                self.assertFalse((output / "newVersion/Demo/same.txt").exists())
                projects = [{"project_name": "Demo", "vcs_type": "压缩包", "show_project_root": True, "diff_result": result}]
                ReportGenerator().generate(result, str(output / "report.html"))
                ReportGenerator().generate_multi(projects, str(output / "multi.html"))
                for name in ("report.html", "multi.html"):
                    html = (output / name).read_text(encoding="utf-8")
                    self.assertIn("旧比较根：old&amp;release/", html)
                    self.assertIn("新比较根：new&amp;release/", html)
                    self.assertNotIn("old&release/config/value.txt", html)
                write_delivery_instructions(projects, str(output / "instructions.txt"))
                instructions = (output / "instructions.txt").read_text(encoding="utf-8-sig")
                self.assertIn("Demo/rename-old.txt", instructions)
                self.assertIn("旧比较根：old&release/", instructions)
                roots = vcs._tmp_old, vcs._tmp_new
                vcs.cleanup()
                self.assertTrue(all(not os.path.exists(p) for p in roots))

    def test_only_one_level_is_removed_even_when_more_levels_are_unique(self):
        vcs = self.vcs({"v1/app/config/a.txt": b"old"}, {"v2/app/config/a.txt": b"new"}, ignore_single_root=True)
        self.assertEqual(["app/config/a.txt"], [f.path for f in vcs.get_changed_files()])

    def test_implicit_and_explicit_root_directory_entries_both_work(self):
        vcs = self.vcs({"v1/": b"", "v1/a.txt": b"same"}, {"v2/a.txt": b"same"}, ignore_single_root=True)
        self.assertEqual([], vcs.get_changed_files())

    def test_exclusions_are_relative_to_inner_root_and_preserve_one_sided_rename(self):
        vcs = self.vcs({"v1/config/a.txt": b"old", "v1/live.txt": b"rename"},
                       {"v2/config/a.txt": b"new", "v2/ignored/copy.txt": b"rename"}, ignore_single_root=True)
        vcs.set_exclude_patterns(["config/**", "ignored/**"])
        result = DiffEngine(vcs).generate_diff("old", "new")
        self.assertEqual({"live.txt": "D"}, {f.file_path: f.report_type for f in result.files})

    def test_no_guessing_with_flat_multiple_or_empty_roots_and_cleanup_on_failure(self):
        invalid = [
            {}, {"value.txt": b"x"},
            {"a/x.txt": b"x", "b/y.txt": b"y"},
            {"a/x.txt": b"x", "readme.txt": b"y"},
            {"a/x.txt": b"x", "empty/": b""},
        ]
        for suffix in (".zip", ".tar"):
            for index, entries in enumerate(invalid):
                with self.subTest(suffix=suffix, index=index):
                    with self.assertRaisesRegex(ValueError, "新压缩包.*顶层必须只有一个文件夹"):
                        self.vcs({"old/x.txt": b"x"}, entries, suffix, ignore_single_root=True)
                    self.assertEqual([], list((self.root / "runtime").glob("cmp_*")))

    def test_archive_validation_still_rejects_traversal_before_selecting_root(self):
        with self.assertRaises(ValueError):
            self.vcs({"v1/a.txt": b"a"}, {"v2/../escape.txt": b"bad"}, ignore_single_root=True)

    def test_permissions_are_rebased_and_preserved_for_unchanged_bytes(self):
        for suffix in (".zip", ".tar"):
            with self.subTest(suffix=suffix):
                old, new = self.root / ("old" + suffix), self.root / ("new" + suffix)
                write_archive(old, {"v1/run.sh": b"echo ok\n"}, mode=0o644)
                write_archive(new, {"v2/run.sh": b"echo ok\n"}, mode=0o755)
                vcs = ArchiveVCS(str(old), str(new), ignore_single_root=True)
                self.addCleanup(vcs.cleanup)
                result = DiffEngine(vcs).generate_diff("old", "new")
                self.assertEqual(["run.sh"], [f.file_path for f in result.files])
                self.assertEqual("0644", result.files[0].old_mode)
                self.assertEqual("0755", result.files[0].new_mode)
                self.assertTrue(result.files[0].new_executable)
                self.assertIn("0644 → 0755", result.files[0].metadata_changes[0])

    def test_empty_inner_directory_replaced_by_file_keeps_deletion_instruction(self):
        vcs = self.vcs({"v1/slot/": b""}, {"v2/slot": b"file"}, ignore_single_root=True)
        result = DiffEngine(vcs).generate_diff("old", "new")
        self.assertEqual(["slot"], result.required_directory_deletions)

    def test_legacy_tasks_keep_default_and_selected_task_retains_option(self):
        app = CompareToolApp.__new__(CompareToolApp)
        task = {"project_name": "Demo", "vcs_type": "archive", "old_version": "old.zip", "new_version": "new.zip"}
        self.assertFalse(app._normalize_loaded_multi_tasks([task])[0]["ignore_archive_root"])
        selected = app._normalize_loaded_multi_tasks([dict(task, ignore_archive_root=True)])[0]
        self.assertTrue(selected["ignore_archive_root"])
        with mock.patch("main.ArchiveVCS") as archive:
            app._create_vcs_for_task(selected)
            archive.assert_called_once_with("old.zip", "new.zip", ignore_single_root=True)

    def test_gui_option_visibility_task_roundtrip_and_single_generation_snapshot(self):
        old, new = self.root / "old.zip", self.root / "new.zip"
        write_archive(old, {"v1/a.txt": b"old"})
        write_archive(new, {"v2/a.txt": b"new"})
        with mock.patch("main._load_config", return_value={}), mock.patch("main._CONFIG_LOAD_FAILURE", None), \
                mock.patch("main.CONFIG_DIR", str(self.root)), mock.patch("main.CONFIG_FILE", str(self.root / "config.json")), \
                mock.patch("main.FileExporter.recover_transactions", return_value=[]):
            app = CompareToolApp()
            try:
                app.root.withdraw()
                self.assertEqual("", app.archive_options_frame.winfo_manager())
                app.vcs_var.set("archive")
                self.assertEqual("grid", app.archive_options_frame.winfo_manager())
                app.old_version_var.set(str(old))
                app.new_version_var.set(str(new))
                app._do_update_output_paths()
                self.assertFalse(app.ignore_archive_root_var.get())
                app.ignore_archive_root_var.set(True)
                app._add_or_update_multi_task()
                self.assertTrue(app._multi_tasks[0]["ignore_archive_root"])
                app.ignore_archive_root_var.set(False)
                app.multi_task_tree.selection_set("0")
                app._edit_multi_task()
                self.assertTrue(app.ignore_archive_root_var.get())
                app._cancel_edit_task()
                app.output_dir_var.set(str(self.root / "output"))
                app._do_update_output_paths()
                with mock.patch("main.messagebox.askyesno", return_value=True), mock.patch("main.threading.Thread") as thread:
                    app._generate()
                    self.assertTrue(thread.call_args.kwargs["args"][-1])
                    thread.return_value.start.assert_called_once()
                app._set_generating(False)
                app.vcs_var.set("folder")
                self.assertEqual("", app.archive_options_frame.winfo_manager())
                self.assertFalse(app._ignore_archive_root())
                app.vcs_var.set("archive")
                app.new_version_var.set(str(new))
                app._do_update_output_paths()
                self.assertTrue(app.ignore_archive_root_var.get())
            finally:
                app.root.destroy()


if __name__ == "__main__":
    unittest.main()
