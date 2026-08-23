import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

import main
from diff_engine import DiffEngine, DiffResult, FileDiff
from file_exporter import FileExporter
from main import CompareToolApp
from vcs.archive_vcs import ArchiveVCS
from vcs.base import ChangedFile, ChangeType
from vcs.folder_vcs import FolderVCS
from vcs.git_vcs import GitVCS, _unescape_git_path
from vcs.multi_version_vcs import SVNMultiVersionVCS
from vcs.svn_vcs import SVNVCS


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


def write_text(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(value)


class ExportTransactionRegressionTests(unittest.TestCase):
    @staticmethod
    def _empty_exporter():
        result = DiffResult("demo", "demo", "Fake", "old", "new", files=[])
        return FileExporter(result, object())

    def test_fresh_nested_project_targets_create_missing_parents(self):
        with project_temp_dir() as root:
            old_stage = os.path.join(root, "old-stage")
            new_stage = os.path.join(root, "new-stage")
            os.makedirs(old_stage)
            os.makedirs(new_stage)
            write_text(os.path.join(old_stage, "old.txt"), "old")
            write_text(os.path.join(new_stage, "new.txt"), "new")
            old_target = os.path.join(root, "batch", "oldVersion", "Demo")
            new_target = os.path.join(root, "batch", "newVersion", "Demo")

            FileExporter._replace_outputs([
                (old_stage, old_target),
                (new_stage, new_target),
            ])

            self.assertTrue(os.path.isfile(os.path.join(old_target, "old.txt")))
            self.assertTrue(os.path.isfile(os.path.join(new_target, "new.txt")))

    def test_report_instructions_and_both_exports_roll_back_as_one_group(self):
        with project_temp_dir() as root:
            targets = [
                os.path.join(root, "oldVersion"),
                os.path.join(root, "newVersion"),
                os.path.join(root, "report.html"),
                os.path.join(root, "上线操作说明.txt"),
            ]
            stages = [
                os.path.join(root, "old-stage"),
                os.path.join(root, "new-stage"),
                os.path.join(root, "report-stage.html"),
                os.path.join(root, "instructions-stage.txt"),
            ]
            os.makedirs(targets[0])
            os.makedirs(targets[1])
            os.makedirs(stages[0])
            os.makedirs(stages[1])
            write_text(os.path.join(targets[0], "value.txt"), "old-original")
            write_text(os.path.join(targets[1], "value.txt"), "new-original")
            write_text(os.path.join(stages[0], "value.txt"), "old-staged")
            write_text(os.path.join(stages[1], "value.txt"), "new-staged")
            write_text(targets[2], "report-original")
            write_text(stages[2], "report-staged")
            write_text(targets[3], "instructions-original")
            write_text(stages[3], "instructions-staged")

            real_replace = os.replace
            replace_count = 0

            def fail_report_install(src, dst):
                nonlocal replace_count
                replace_count += 1
                if replace_count == 8:
                    raise OSError("instructions locked")
                return real_replace(src, dst)

            with mock.patch("file_exporter.os.replace", side_effect=fail_report_install):
                with self.assertRaisesRegex(OSError, "instructions locked"):
                    FileExporter._replace_outputs(list(zip(stages, targets)))

            with open(os.path.join(targets[0], "value.txt"), encoding="utf-8") as stream:
                self.assertEqual("old-original", stream.read())
            with open(os.path.join(targets[1], "value.txt"), encoding="utf-8") as stream:
                self.assertEqual("new-original", stream.read())
            with open(targets[2], encoding="utf-8") as stream:
                self.assertEqual("report-original", stream.read())
            with open(targets[3], encoding="utf-8") as stream:
                self.assertEqual("instructions-original", stream.read())

    def test_single_generation_keeps_report_and_exports_in_same_transaction(self):
        with project_temp_dir() as root:
            source_old = os.path.join(root, "source-old")
            source_new = os.path.join(root, "source-new")
            write_text(os.path.join(source_old, "value.txt"), "source-old")
            write_text(os.path.join(source_new, "value.txt"), "source-new")
            report = os.path.join(root, "Demo_diff.html")
            instructions = os.path.join(root, "上线操作说明.txt")
            old_target = os.path.join(root, "oldVersion", "Demo")
            new_target = os.path.join(root, "newVersion", "Demo")
            write_text(report, "report-original")
            write_text(instructions, "instructions-original")
            write_text(os.path.join(old_target, "value.txt"), "old-original")
            write_text(os.path.join(new_target, "value.txt"), "new-original")

            app = CompareToolApp.__new__(CompareToolApp)
            app.root = mock.Mock()
            app.root.after.return_value = None
            real_replace = os.replace
            replace_count = 0

            def fail_report_install(src, dst):
                nonlocal replace_count
                replace_count += 1
                if replace_count == 8:
                    raise OSError("instructions install failed")
                return real_replace(src, dst)

            with mock.patch("file_exporter.os.replace", side_effect=fail_report_install):
                app._do_generate(
                    source_new,
                    "folder",
                    source_old,
                    source_new,
                    "Demo",
                    [],
                    True,
                    True,
                    report,
                    os.path.join(root, "oldVersion"),
                    os.path.join(root, "newVersion"),
                )

            with open(report, encoding="utf-8") as stream:
                self.assertEqual("report-original", stream.read())
            with open(instructions, encoding="utf-8") as stream:
                self.assertEqual("instructions-original", stream.read())
            with open(os.path.join(old_target, "value.txt"), encoding="utf-8") as stream:
                self.assertEqual("old-original", stream.read())
            with open(os.path.join(new_target, "value.txt"), encoding="utf-8") as stream:
                self.assertEqual("new-original", stream.read())

    def test_single_generation_creates_delivery_instructions_with_outputs(self):
        with project_temp_dir() as root:
            source_old = os.path.join(root, "source-old")
            source_new = os.path.join(root, "source-new")
            write_text(os.path.join(source_old, "Old.java"), "old")
            os.makedirs(source_new)
            report = os.path.join(root, "Demo_diff.html")

            app = CompareToolApp.__new__(CompareToolApp)
            app.root = mock.Mock()
            app.root.after.return_value = None
            app._do_generate(
                source_new,
                "folder",
                source_old,
                source_new,
                "Demo",
                [],
                True,
                True,
                report,
                os.path.join(root, "oldVersion"),
                os.path.join(root, "newVersion"),
            )

            instructions = os.path.join(root, "上线操作说明.txt")
            self.assertTrue(os.path.isfile(report))
            self.assertTrue(os.path.isfile(instructions))
            with open(instructions, encoding="utf-8-sig") as stream:
                text = stream.read()
            self.assertIn("[删除文件] Demo/Old.java", text)

    def test_multi_generation_replaces_roots_and_removes_stale_projects(self):
        with project_temp_dir() as root:
            def task(name):
                old_dir = os.path.join(root, f"{name}-old")
                new_dir = os.path.join(root, f"{name}-new")
                write_text(os.path.join(old_dir, "value.txt"), f"{name}-old")
                write_text(os.path.join(new_dir, "value.txt"), f"{name}-new")
                return {
                    "project_name": name,
                    "vcs_type": "folder",
                    "project_path": "",
                    "old_version": old_dir,
                    "new_version": new_dir,
                    "exclude_rules": "",
                    "show_project_root": True,
                    "show_full_context": True,
                }

            task_a = task("A")
            task_b = task("B")
            app = CompareToolApp.__new__(CompareToolApp)
            app.root = mock.Mock()
            app.root.after.return_value = None
            old_target = os.path.join(root, "oldVersion")
            new_target = os.path.join(root, "newVersion")
            report = os.path.join(root, "multi.html")

            app._do_generate_multi(
                [task_a, task_b], report, old_target, new_target
            )
            self.assertTrue(os.path.isdir(os.path.join(old_target, "B")))
            self.assertTrue(os.path.isdir(os.path.join(new_target, "B")))

            app._do_generate_multi([task_a], report, old_target, new_target)

            self.assertTrue(os.path.isdir(os.path.join(old_target, "A")))
            self.assertTrue(os.path.isdir(os.path.join(new_target, "A")))
            self.assertFalse(os.path.exists(os.path.join(old_target, "B")))
            self.assertFalse(os.path.exists(os.path.join(new_target, "B")))

    def test_second_stage_creation_failure_cleans_first_stage(self):
        with project_temp_dir() as root:
            target_old = os.path.join(root, "old")
            target_new = os.path.join(root, "new")
            first_stage = os.path.join(root, "first-stage")
            os.makedirs(first_stage)
            exporter = self._empty_exporter()

            with mock.patch.object(
                exporter,
                "_make_stage_dir",
                side_effect=[first_stage, OSError("cannot create second stage")],
            ):
                with self.assertRaisesRegex(OSError, "second stage"):
                    exporter.prepare_export(target_old, target_new)

            self.assertFalse(os.path.exists(first_stage))

    def test_ntfs_ads_component_is_rejected(self):
        with project_temp_dir() as root:
            result = DiffResult(
                "demo", "demo", "Fake", "old", "new",
                files=[FileDiff("dir/base.txt:payload", ChangeType.ADDED)],
            )
            vcs = type("BytesVCS", (), {
                "get_file_content_bytes": lambda self, version, path: b"payload",
            })()

            with self.assertRaisesRegex(RuntimeError, "Windows 非法字符"):
                FileExporter(result, vcs).export(
                    os.path.join(root, "old"), os.path.join(root, "new")
                )

    def test_interrupted_transaction_journal_restores_original_outputs(self):
        with project_temp_dir() as root:
            token = "a" * 32
            states = []
            for name in ("oldVersion", "newVersion"):
                target = os.path.join(root, name)
                stage = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=root)
                write_text(os.path.join(target, "value.txt"), f"{name}-original")
                write_text(os.path.join(stage, "value.txt"), f"{name}-staged")
                states.append({
                    "stage": stage,
                    "target": target,
                    "backup": f"{target}.comparetool_backup_{token}",
                    "had_target": True,
                    "installed": False,
                })
            journal = FileExporter._create_transaction_journal(states, token)

            os.replace(states[0]["target"], states[0]["backup"])
            os.replace(states[1]["target"], states[1]["backup"])
            os.replace(states[0]["stage"], states[0]["target"])

            recovered = FileExporter.recover_transactions(root)

            self.assertEqual([journal], recovered)
            for state, name in zip(states, ("oldVersion", "newVersion")):
                with open(os.path.join(state["target"], "value.txt"), encoding="utf-8") as stream:
                    self.assertEqual(f"{name}-original", stream.read())
                self.assertFalse(os.path.lexists(state["backup"]))
                self.assertFalse(os.path.lexists(state["stage"]))
            self.assertFalse(os.path.exists(journal))

    def test_completed_transaction_journal_finishes_backup_cleanup(self):
        with project_temp_dir() as root:
            token = "b" * 32
            states = []
            for name in ("oldVersion", "newVersion"):
                target = os.path.join(root, name)
                stage = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=root)
                write_text(os.path.join(target, "value.txt"), "original")
                write_text(os.path.join(stage, "value.txt"), "committed")
                states.append({
                    "stage": stage,
                    "target": target,
                    "backup": f"{target}.comparetool_backup_{token}",
                    "had_target": True,
                    "installed": False,
                })
            journal = FileExporter._create_transaction_journal(states, token)
            for state in states:
                os.replace(state["target"], state["backup"])
            for state in states:
                os.replace(state["stage"], state["target"])

            FileExporter.recover_transactions(root)

            for state in states:
                with open(os.path.join(state["target"], "value.txt"), encoding="utf-8") as stream:
                    self.assertEqual("committed", stream.read())
                self.assertFalse(os.path.lexists(state["backup"]))
            self.assertFalse(os.path.exists(journal))

    def test_interrupted_delivery_instruction_is_accepted_by_recovery_validator(self):
        with project_temp_dir() as root:
            target = os.path.join(root, "上线操作说明.txt")
            fd, stage = tempfile.mkstemp(
                prefix=".comparetool_delivery_", suffix=".txt", dir=root
            )
            os.close(fd)
            write_text(target, "original")
            write_text(stage, "staged")
            token = "c" * 32
            state = {
                "stage": stage,
                "target": target,
                "backup": f"{target}.comparetool_backup_{token}",
                "had_target": True,
                "installed": False,
            }
            journal = FileExporter._create_transaction_journal([state], token)
            os.replace(target, state["backup"])

            recovered = FileExporter.recover_transactions(root)

            self.assertEqual([journal], recovered)
            with open(target, encoding="utf-8") as stream:
                self.assertEqual("original", stream.read())
            self.assertFalse(os.path.exists(stage))
            self.assertFalse(os.path.exists(state["backup"]))

    def test_orphan_pretransaction_stages_are_cleaned_but_exports_are_not_scanned(self):
        with project_temp_dir() as root:
            batch = os.path.join(root, "20260824")
            os.makedirs(batch)
            orphan_dir = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=batch)
            report_fd, orphan_report = tempfile.mkstemp(
                prefix=".comparetool_report_", suffix=".html", dir=batch
            )
            os.close(report_fd)
            delivery_fd, orphan_delivery = tempfile.mkstemp(
                prefix=".comparetool_delivery_", suffix=".txt", dir=batch
            )
            os.close(delivery_fd)
            write_text(os.path.join(orphan_dir, "source.java"), "source")

            export_like_name = os.path.join(
                root, "oldVersion", ".comparetool_stage_abcdefgh", "source.java"
            )
            write_text(export_like_name, "real exported source")

            project_pairs = self._empty_exporter().prepare_export(
                os.path.join(batch, "oldVersion"),
                os.path.join(batch, "newVersion"),
                project_name="Demo",
            )
            project_stage_roots = {
                os.path.dirname(stage) for stage, _target in project_pairs
            }
            self.assertEqual(2, len(project_stage_roots))
            for stage_root in project_stage_roots:
                self.assertEqual(batch, os.path.dirname(stage_root))
                self.assertTrue(
                    os.path.basename(stage_root).startswith(".comparetool_stage_")
                )

            FileExporter.recover_transactions(root, include_direct_children=True)

            self.assertFalse(os.path.exists(orphan_dir))
            self.assertFalse(os.path.exists(orphan_report))
            self.assertFalse(os.path.exists(orphan_delivery))
            for stage_root in project_stage_roots:
                self.assertFalse(os.path.exists(stage_root))
            self.assertTrue(os.path.isfile(export_like_name))

    def test_multi_project_nested_stages_are_recoverable(self):
        with project_temp_dir() as root:
            token = "c" * 32
            old_stage_root = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=root)
            new_stage_root = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=root)
            states = []
            for stage_root, side in ((old_stage_root, "oldVersion"), (new_stage_root, "newVersion")):
                stage = os.path.join(stage_root, "Demo")
                target = os.path.join(root, side, "Demo")
                os.makedirs(stage)
                write_text(os.path.join(stage, "value.txt"), "staged")
                write_text(os.path.join(target, "value.txt"), "original")
                states.append({
                    "stage": stage,
                    "target": target,
                    "backup": f"{target}.comparetool_backup_{token}",
                    "had_target": True,
                    "installed": False,
                })
            journal = FileExporter._create_transaction_journal(states, token)
            for state in states:
                os.replace(state["target"], state["backup"])
            os.replace(states[0]["stage"], states[0]["target"])

            FileExporter.recover_transactions(root, raise_on_error=True)

            for state in states:
                with open(os.path.join(state["target"], "value.txt"), encoding="utf-8") as stream:
                    self.assertEqual("original", stream.read())
                self.assertFalse(os.path.exists(state["backup"]))
            self.assertFalse(os.path.exists(old_stage_root))
            self.assertFalse(os.path.exists(new_stage_root))
            self.assertFalse(os.path.exists(journal))


class ArchiveAndFolderRegressionTests(unittest.TestCase):
    def test_tar_root_directory_member_is_a_noop(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "root-entry.tar")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with tarfile.open(archive, "w") as stream:
                root_entry = tarfile.TarInfo("./")
                root_entry.type = tarfile.DIRTYPE
                stream.addfile(root_entry)
                member = tarfile.TarInfo("./safe.txt")
                member.size = 4
                stream.addfile(member, io.BytesIO(b"safe"))

            ArchiveVCS.__new__(ArchiveVCS)._extract_tar(archive, dest)

            with open(os.path.join(dest, "safe.txt"), "rb") as stream:
                self.assertEqual(b"safe", stream.read())

    def test_zip_member_count_limit_blocks_extraction(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "many.zip")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("one.txt", b"1")
                stream.writestr("two.txt", b"2")

            with mock.patch.object(ArchiveVCS, "MAX_ARCHIVE_MEMBERS", 1):
                with self.assertRaisesRegex(ValueError, "成员过多"):
                    ArchiveVCS.__new__(ArchiveVCS)._extract_zip(archive, dest)

            self.assertEqual([], os.listdir(dest))

    def test_zip_compression_ratio_limit_blocks_extraction(self):
        with project_temp_dir() as root:
            archive = os.path.join(root, "ratio.zip")
            dest = os.path.join(root, "dest")
            os.makedirs(dest)
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
                stream.writestr("large.txt", b"A" * 4096)

            with mock.patch.object(ArchiveVCS, "MAX_COMPRESSION_RATIO", 2):
                with self.assertRaisesRegex(ValueError, "展开比例过高"):
                    ArchiveVCS.__new__(ArchiveVCS)._extract_zip(archive, dest)

            self.assertEqual([], os.listdir(dest))

    def test_folder_file_symlink_cannot_read_outside_root(self):
        with project_temp_dir() as root:
            old_dir = os.path.join(root, "old")
            new_dir = os.path.join(root, "new")
            os.makedirs(old_dir)
            os.makedirs(new_dir)
            outside = os.path.join(root, "secret.txt")
            write_text(outside, "secret")
            link = os.path.join(new_dir, "linked.txt")
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"当前 Windows 未允许创建测试符号链接: {exc}")

            with self.assertRaisesRegex(RuntimeError, "符号链接"):
                FolderVCS(old_dir, new_dir).get_changed_files()


class VCSRegressionTests(unittest.TestCase):
    def test_rename_crossing_exclude_boundary_becomes_add_or_delete(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = ["target/**"]

        moved_out = vcs._filter_files([
            ChangedFile("target/a.txt", ChangeType.RENAMED, old_path="src/a.txt")
        ])
        moved_in = vcs._filter_files([
            ChangedFile("src/a.txt", ChangeType.RENAMED, old_path="target/a.txt")
        ])

        self.assertEqual(
            [ChangedFile("src/a.txt", ChangeType.DELETED)], moved_out
        )
        self.assertEqual(
            [ChangedFile("src/a.txt", ChangeType.ADDED)], moved_in
        )

    def test_git_c_style_path_escapes_are_decoded(self):
        raw = '"dir\\tline\\nquote\\"-slash\\\\-\\344\\270\\255.txt"'
        self.assertEqual(
            'dir\tline\nquote"-slash\\-中.txt',
            _unescape_git_path(raw),
        )

    def test_git_type_change_fails_instead_of_exporting_wrong_type(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs.exclude_patterns = []
        vcs._run = lambda args: "T\tchanged.txt\n"

        with self.assertRaisesRegex(RuntimeError, "文件类型发生变化"):
            vcs.get_changed_files("old", "new")

    def test_git_attributes_override_core_autocrlf(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs._get_checkout_attributes = mock.Mock()
        vcs._git_config_value = mock.Mock(return_value="true")

        vcs._get_checkout_attributes.return_value = {"text": "set", "eol": "lf"}
        self.assertFalse(vcs._checkout_uses_crlf("v", "script.sh", b"a\nb\n"))

        vcs._get_checkout_attributes.return_value = {"text": "unset", "eol": "unspecified"}
        self.assertFalse(vcs._checkout_uses_crlf("v", "payload.dat", b"a\nb\n"))

        vcs._get_checkout_attributes.return_value = {"text": "set", "eol": "crlf"}
        self.assertTrue(vcs._checkout_uses_crlf("v", "windows.txt", b"a\nb\n"))

    def test_svn_explicit_eol_styles_are_applied(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs.get_file_content_raw_bytes = lambda version, path: b"a\r\nb\rc\n"

        for style, expected in (
            ("LF", b"a\nb\nc\n"),
            ("CRLF", b"a\r\nb\r\nc\r\n"),
            ("CR", b"a\rb\rc\r"),
        ):
            with self.subTest(style=style):
                vcs._get_eol_style = lambda version, path, value=style: value
                self.assertEqual(expected, vcs.get_file_content_bytes("12", "file.txt"))

    def test_svn_multi_history_xml_keeps_file_identity_and_ignores_other_projects(self):
        vcs = SVNMultiVersionVCS.__new__(SVNMultiVersionVCS)
        vcs._project_repo_path = "/trunk/Demo"
        history = vcs._parse_svn_history("""<?xml version="1.0"?>
<log><logentry revision="12"><paths>
  <path action="D" kind="file">/trunk/Demo/old.txt</path>
  <path action="A" kind="file" copyfrom-path="/trunk/Demo/old.txt" copyfrom-rev="11">/trunk/Demo/new.txt</path>
  <path action="M" kind="file">/trunk/Other/ignored.txt</path>
</paths></logentry></log>""")

        self.assertEqual(2, len(history[12]))
        self.assertEqual("old.txt", history[12][0].path)
        self.assertEqual("new.txt", history[12][1].path)
        self.assertEqual("old.txt", history[12][1].copyfrom_path)


class DiffAndConfigRegressionTests(unittest.TestCase):
    def test_renamed_format_only_change_has_visible_explanation(self):
        class RenameVCS:
            project_path = "demo"

            def get_changed_files(self, old, new):
                return [ChangedFile("new.txt", ChangeType.RENAMED, old_path="old.txt")]

            def get_file_content_raw_bytes(self, version, path):
                return "中文\n".encode("gbk") if version == "old" else "中文\n".encode("utf-8")

            def get_file_content_bytes(self, version, path):
                return self.get_file_content_raw_bytes(version, path)

            def get_file_content(self, version, path):
                raw = self.get_file_content_raw_bytes(version, path)
                return raw.decode("gbk" if version == "old" else "utf-8")

        file_diff = DiffEngine(RenameVCS()).generate_diff("old", "new").files[0]

        self.assertEqual("R", file_diff.report_type)
        self.assertTrue(file_diff.format_only)
        self.assertEqual(0, file_diff.added_lines)
        self.assertEqual(0, file_diff.deleted_lines)
        self.assertIn("重命名，同时发生格式变化", file_diff.side_by_side_html)
        self.assertIn("编码：GB18030/GBK → UTF-8", file_diff.side_by_side_html)

    def test_malformed_config_tasks_are_ignored_and_valid_task_is_normalized(self):
        app = CompareToolApp.__new__(CompareToolApp)
        valid = {
            "project_name": " Demo ",
            "vcs_type": "git",
            "project_path": " C:/repo ",
            "old_version": " old ",
            "new_version": " new ",
        }

        tasks = app._normalize_loaded_multi_tasks([
            {},
            {"vcs_type": "mystery"},
            valid,
            dict(valid, project_name="demo"),
        ])

        self.assertEqual(1, len(tasks))
        self.assertEqual("Demo", tasks[0]["project_name"])
        self.assertEqual("git", tasks[0]["vcs_type"])
        self.assertEqual("C:/repo", tasks[0]["project_path"])

    def test_legacy_multi_version_task_is_migrated_to_file_endpoints(self):
        app = CompareToolApp.__new__(CompareToolApp)
        tasks = app._normalize_loaded_multi_tasks([{
            "project_name": "Demo",
            "vcs_type": "git_multi",
            "project_path": "C:/repo",
            "old_version": "abc123, def456",
            "new_version": "基线 + 选中版本",
        }])

        self.assertEqual(1, len(tasks))
        self.assertEqual("文件级首尾端点", tasks[0]["new_version"])

    def test_unknown_task_vcs_is_not_silently_treated_as_svn(self):
        app = CompareToolApp.__new__(CompareToolApp)
        with self.assertRaisesRegex(RuntimeError, "不支持"):
            app._create_vcs_for_task({"vcs_type": "mystery"})

    def test_stale_version_failure_does_not_touch_new_project_ui(self):
        app = CompareToolApp.__new__(CompareToolApp)
        app._version_request_id = 2
        app.vcs_var = mock.Mock(get=mock.Mock(return_value="svn"))
        app.dir_entry = mock.Mock(get=mock.Mock(return_value="C:/new"))
        app.root = mock.Mock()
        app.root.after.side_effect = lambda delay, callback: callback()
        app.status_var = mock.Mock()

        with mock.patch("main.GitVCS", side_effect=RuntimeError("old project failed")), \
                mock.patch("main.messagebox.showerror") as show_error:
            app._do_fetch_versions("C:/old", "git", 1)

        show_error.assert_not_called()
        app.status_var.set.assert_not_called()

    def test_unknown_version_request_is_not_silently_treated_as_svn(self):
        app = CompareToolApp.__new__(CompareToolApp)
        app._version_request_id = 1
        app.vcs_var = mock.Mock(get=mock.Mock(return_value="mystery"))
        app.dir_entry = mock.Mock(get=mock.Mock(return_value="C:/repo"))
        app.root = mock.Mock()
        app.root.after.side_effect = lambda delay, callback: callback()
        app.status_var = mock.Mock()

        with mock.patch("main.SVNVCS") as svn, \
                mock.patch("main.messagebox.showerror") as show_error:
            app._do_fetch_versions("C:/repo", "mystery", 1)

        svn.assert_not_called()
        show_error.assert_called_once()
        self.assertIn("不支持", show_error.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
