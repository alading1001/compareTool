import os
import json
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from diff_engine import DiffEngine, DiffResult, FileDiff, _DecodedText
from file_exporter import FileExporter
from report_generator import ReportGenerator
from stage_ownership import remove_ownership_marker
from vcs.base import ChangeType
from vcs.folder_vcs import FolderVCS
from vcs.multi_version_vcs import (
    GitMultiVersionVCS,
    SVNMultiVersionVCS,
    _EndpointPlanner,
    _HistoryChange,
    _LogicalFile,
    _MultiVersionFolderDelegate,
)


@contextmanager
def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    with tempfile.TemporaryDirectory(dir=".tmp") as directory:
        yield os.path.abspath(directory)


def write_bytes(path: str, payload: bytes):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(payload)


class TerminalVCSAndReportTests(unittest.TestCase):
    def test_exclude_tree_requires_one_rule_to_cover_both_depths(self):
        vcs = FolderVCS.__new__(FolderVCS)
        vcs.exclude_patterns = ["foo/*", "foo/*/*"]

        self.assertFalse(vcs._should_prune_directory("foo"))
        self.assertFalse(vcs._is_excluded("foo/a/b/c.txt"))

    def test_exclude_tree_never_uses_predictable_probe_names(self):
        vcs = FolderVCS.__new__(FolderVCS)
        for pattern in ("*comparetool*", "*probe*"):
            with self.subTest(pattern=pattern):
                vcs.exclude_patterns = [pattern]
                self.assertFalse(vcs._should_prune_directory("business"))
                self.assertFalse(vcs._is_excluded("business/order.txt"))

    def test_exclude_tree_prunes_only_provable_recursive_patterns(self):
        vcs = FolderVCS.__new__(FolderVCS)
        cases = (
            (["target/**"], "target", True),
            (["target/**"], "nested/target", False),
            (["**/target/**"], "nested/target", True),
            (["foo/*/**"], "foo/module", True),
            (["foo/*"], "foo/module", False),
        )
        for patterns, directory, expected in cases:
            with self.subTest(patterns=patterns, directory=directory):
                vcs.exclude_patterns = patterns
                self.assertEqual(
                    expected, vcs._should_prune_directory(directory)
                )

    def test_endpoint_budget_rejects_large_non_rename_files_before_writing(self):
        delegate = _MultiVersionFolderDelegate.__new__(
            _MultiVersionFolderDelegate
        )
        delegate._tmp_root = os.getcwd()
        delegate._content_vcs = SimpleNamespace(
            get_file_size=lambda version, path: 60
        )
        delegate.MAX_ENDPOINT_SOURCE_BYTES = 100
        entity = _LogicalFile(
            1,
            selected=True,
            old_version="1",
            old_path="old.txt",
            new_version="2",
            new_path="new.txt",
        )

        with self.assertRaisesRegex(RuntimeError, "源文件总字节数"):
            delegate._reserve_endpoint_budget(
                [(entity, entity.old_path, entity.new_path)]
            )

    def test_history_budget_covers_pure_add_modify_records(self):
        planner = _EndpointPlanner()
        planner.MAX_HISTORY_CHANGES = 1

        with self.assertRaisesRegex(RuntimeError, "变更记录数"):
            planner.apply(
                [_HistoryChange("A", "a.txt"), _HistoryChange("M", "b.txt")],
                selected=True,
                old_version="old",
                new_version="new",
            )

    def test_git_and_svn_raw_output_caps_apply_before_parsing(self):
        with mock.patch.object(
            GitMultiVersionVCS, "MAX_GIT_COMMAND_OUTPUT_BYTES", 8
        ):
            with self.assertRaisesRegex(RuntimeError, "原始字节"):
                GitMultiVersionVCS._parse_git_changes(b"A\x00too-long.txt\x00")

        svn = SVNMultiVersionVCS.__new__(SVNMultiVersionVCS)
        svn._project_repo_path = "/demo"
        svn.MAX_SVN_COMMAND_OUTPUT_BYTES = 8
        with self.assertRaisesRegex(RuntimeError, "XML 超过"):
            svn._parse_svn_history_bytes(b"<log><logentry/></log>")

    def test_snapshot_open_handle_must_match_precaptured_file_id(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "source")
            alternate = os.path.join(root, "alternate.txt")
            original = os.path.join(source, "value.txt")
            write_bytes(original, b"same")
            write_bytes(alternate, b"evil")
            metadata = os.stat(original)
            os.utime(
                alternate,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
            )
            vcs = FolderVCS(source, os.path.join(root, "unused"), snapshot=True)
            capture = vcs._capture_directory(source)
            real_open = __import__(
                "path_safety", fromlist=["open_regular_file_no_links"]
            ).open_regular_file_no_links

            @contextmanager
            def open_replaced(path):
                with real_open(alternate if path == original else path) as stream:
                    yield stream

            with mock.patch(
                "vcs.folder_vcs.open_regular_file_no_links",
                side_effect=open_replaced,
            ):
                with self.assertRaisesRegex(RuntimeError, "复制前发生变化"):
                    vcs._snapshot_directory(source, "identity_probe_", capture, 0)
            vcs.cleanup()

    def test_form_feed_is_not_treated_as_trailing_newline_format_only(self):
        engine = DiffEngine.__new__(DiffEngine)
        for old_text in ("a\f", "a\v", "a\x85", "a\u2028", "a\u2029"):
            with self.subTest(old_text=repr(old_text)):
                details = engine._format_only_details(
                    _DecodedText(old_text, "UTF-8"),
                    _DecodedText("a\n", "UTF-8"),
                    old_text.encode("utf-8"),
                    b"a\n",
                )
                self.assertIsNone(details)

    def test_manifest_budget_counts_htmlsafe_json_expansion(self):
        item = FileDiff("&" * 20 + ".txt", ChangeType.MODIFIED)
        cost = DiffResult._manifest_item_bytes(item)
        result = DiffResult("demo", "demo", "Fake", "old", "new", [item, item])

        with mock.patch.object(
            DiffResult, "MAX_REPORT_MANIFEST_PATH_BYTES", cost * 2 - 1
        ):
            self.assertEqual(1, len(result.report_manifest_files))

    def test_final_report_limit_is_atomic_and_preserves_existing_output(self):
        with project_temp_dir() as root:
            report = os.path.join(root, "report.html")
            write_bytes(report, b"existing")
            result = DiffResult("demo", "demo", "Fake", "old", "new")
            generator = ReportGenerator(os.path.join(os.getcwd(), "templates"))

            with mock.patch.object(
                ReportGenerator, "MAX_REPORT_OUTPUT_BYTES", 128
            ):
                with self.assertRaisesRegex(RuntimeError, "统一大小上限"):
                    generator.generate(result, report)
            with open(report, "rb") as stream:
                self.assertEqual(b"existing", stream.read())


class TerminalTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._old_key_path = os.environ.get(FileExporter.TRANSACTION_KEY_ENV)
        os.makedirs(".tmp", exist_ok=True)
        cls._key_temp = tempfile.TemporaryDirectory(dir=".tmp")
        os.environ[FileExporter.TRANSACTION_KEY_ENV] = os.path.join(
            cls._key_temp.name, "transaction_hmac.key"
        )

    @classmethod
    def tearDownClass(cls):
        if cls._old_key_path is None:
            os.environ.pop(FileExporter.TRANSACTION_KEY_ENV, None)
        else:
            os.environ[FileExporter.TRANSACTION_KEY_ENV] = cls._old_key_path
        cls._key_temp.cleanup()
        super().tearDownClass()

    def test_hardlinked_lock_is_rejected_without_modifying_external_file(self):
        with project_temp_dir() as root:
            external = os.path.join(root, "external.bin")
            output = os.path.join(root, "output")
            os.makedirs(output)
            write_bytes(external, b"")
            lock_path = os.path.join(output, ".comparetool_transaction.lock")
            os.link(external, lock_path)

            with self.assertRaisesRegex(RuntimeError, "锁文件身份无效"):
                with FileExporter._transaction_lock(output):
                    pass
            with open(external, "rb") as stream:
                self.assertEqual(b"", stream.read())

    def test_trusted_root_is_checked_before_lock_creation(self):
        with project_temp_dir() as root:
            batch = os.path.join(root, "alias", "batch")
            stage = os.path.join(root, "stage.txt")
            target = os.path.join(batch, "target.txt")
            write_bytes(stage, b"stage")
            lock_path = os.path.join(batch, ".comparetool_transaction.lock")

            def reject_alias(anchor, path, label):
                if os.path.abspath(path) == os.path.abspath(batch):
                    raise ValueError("ancestor junction")

            with mock.patch(
                "file_exporter.ensure_no_link_components",
                side_effect=reject_alias,
            ):
                with self.assertRaisesRegex(RuntimeError, "ancestor junction"):
                    FileExporter._replace_outputs(
                        [(stage, target)], trusted_root=root
                    )
            self.assertFalse(os.path.exists(lock_path))

    def test_tree_identity_hashes_same_size_same_mtime_content(self):
        with project_temp_dir() as root:
            target = os.path.join(root, "target.txt")
            write_bytes(target, b"first")
            before_stat = os.stat(target)
            before = FileExporter._tree_identity(target)
            write_bytes(target, b"other")
            os.utime(
                target,
                ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
            )

            self.assertNotEqual(before, FileExporter._tree_identity(target))

    def test_stages_are_created_at_trusted_root_not_mutable_target_child(self):
        with project_temp_dir() as root:
            batch = os.path.join(root, "batch")
            target = os.path.join(batch, "oldVersion", "demo")
            stage = FileExporter._make_stage_dir(
                target,
                stage_parent=root,
                trusted_root=root,
            )
            report_stage = FileExporter._make_stage_file(
                os.path.join(batch, "report.html"),
                root,
                ".comparetool_report_",
                ".html",
            )
            try:
                self.assertEqual(
                    os.path.normcase(root),
                    os.path.normcase(os.path.dirname(os.path.dirname(stage))),
                )
                self.assertEqual(
                    os.path.normcase(root),
                    os.path.normcase(os.path.dirname(report_stage)),
                )
                self.assertFalse(
                    os.path.commonpath([stage, batch]) == os.path.abspath(batch)
                )
            finally:
                FileExporter._cleanup_stage(stage)
                FileExporter._cleanup_stage(report_stage)

    def _installed_transaction(self, root: str, token: str):
        stage = os.path.join(root, ".comparetool_report_probe.html")
        target = os.path.join(root, "report.html")
        backup = f"{target}.comparetool_backup_{token}"
        write_bytes(stage, b"new-data")
        write_bytes(target, b"old-data")
        state = {
            "stage": stage,
            "target": target,
            "backup": backup,
            "had_target": True,
            "stage_identity": FileExporter._tree_identity(stage),
            "target_identity": FileExporter._tree_identity(target),
        }
        journal = FileExporter._create_transaction_journal([state], token)
        os.replace(target, backup)
        os.replace(stage, target)
        return state, journal

    def test_recovery_quarantines_then_rejects_postcheck_user_replacement(self):
        with project_temp_dir() as root:
            token = "a" * 32
            state, journal = self._installed_transaction(root, token)
            FileExporter._mark_transaction(journal, "rollback")
            original_phase = FileExporter._recovery_state_phase
            changed = False

            def change_after_check(item):
                nonlocal changed
                phase = original_phase(item)
                if not changed:
                    changed = True
                    write_bytes(item["target"], b"user-new")
                return phase

            with mock.patch.object(
                FileExporter,
                "_recovery_state_phase",
                side_effect=change_after_check,
            ):
                with self.assertRaisesRegex(RuntimeError, "内容元数据已变化"):
                    FileExporter.recover_transactions(root, raise_on_error=True)
            with open(state["target"], "rb") as stream:
                self.assertEqual(b"user-new", stream.read())
            self.assertTrue(os.path.isfile(journal))

    def test_signed_commit_journal_recovers_without_stage_owner_marker(self):
        with project_temp_dir() as root:
            token = "b" * 32
            state, journal = self._installed_transaction(root, token)
            FileExporter._mark_transaction(journal, "commit")
            remove_ownership_marker(FileExporter._stage_owner(state["stage"]))

            FileExporter.recover_transactions(root, raise_on_error=True)

            with open(state["target"], "rb") as stream:
                self.assertEqual(b"new-data", stream.read())
            self.assertFalse(os.path.lexists(state["backup"]))
            self.assertFalse(os.path.exists(journal))

    def test_rollback_recovery_resumes_deterministic_quarantine_cleanup(self):
        with project_temp_dir() as root:
            token = "c" * 32
            state, journal = self._installed_transaction(root, token)
            FileExporter._mark_transaction(journal, "rollback")
            quarantine = FileExporter._quarantine_paths(
                state, token
            )["installed"]
            real_remove = FileExporter._remove_path
            failed = False

            def fail_once(path):
                nonlocal failed
                if os.path.abspath(path) == os.path.abspath(quarantine) and not failed:
                    failed = True
                    raise PermissionError("sharing violation")
                return real_remove(path)

            with mock.patch.object(
                FileExporter, "_remove_path", side_effect=fail_once
            ):
                with self.assertRaisesRegex(RuntimeError, "sharing violation"):
                    FileExporter.recover_transactions(root, raise_on_error=True)

            self.assertTrue(os.path.exists(journal))
            self.assertTrue(os.path.lexists(quarantine))
            self.assertFalse(os.path.lexists(state["backup"]))
            with open(state["target"], "rb") as stream:
                self.assertEqual(b"old-data", stream.read())

            FileExporter.recover_transactions(root, raise_on_error=True)
            self.assertFalse(os.path.lexists(quarantine))
            self.assertFalse(os.path.exists(journal))
            with open(state["target"], "rb") as stream:
                self.assertEqual(b"old-data", stream.read())

    def test_commit_recovery_resumes_deterministic_quarantine_cleanup(self):
        with project_temp_dir() as root:
            token = "d" * 32
            state, journal = self._installed_transaction(root, token)
            FileExporter._mark_transaction(journal, "commit")
            quarantine = FileExporter._quarantine_paths(
                state, token
            )["backup"]
            real_remove = FileExporter._remove_path
            failed = False

            def fail_once(path):
                nonlocal failed
                if os.path.abspath(path) == os.path.abspath(quarantine) and not failed:
                    failed = True
                    raise PermissionError("sharing violation")
                return real_remove(path)

            with mock.patch.object(
                FileExporter, "_remove_path", side_effect=fail_once
            ):
                with self.assertRaisesRegex(RuntimeError, "sharing violation"):
                    FileExporter.recover_transactions(root, raise_on_error=True)

            self.assertTrue(os.path.exists(journal))
            self.assertTrue(os.path.lexists(quarantine))
            self.assertFalse(os.path.lexists(state["backup"]))
            with open(state["target"], "rb") as stream:
                self.assertEqual(b"new-data", stream.read())

            FileExporter.recover_transactions(root, raise_on_error=True)
            self.assertFalse(os.path.lexists(quarantine))
            self.assertFalse(os.path.exists(journal))
            with open(state["target"], "rb") as stream:
                self.assertEqual(b"new-data", stream.read())

    def test_v4_random_quarantine_is_recovered_compatibly(self):
        with project_temp_dir() as root:
            token = "e" * 32
            state, journal = self._installed_transaction(root, token)
            FileExporter._mark_transaction(journal, "rollback")

            with open(journal, encoding="utf-8") as stream:
                payload = json.load(stream)
            payload["version"] = 4
            payload = FileExporter._signed_payload(
                payload,
                FileExporter._load_transaction_key(root, create=False),
            )
            with open(journal, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())

            legacy_quarantine = os.path.join(
                root, f".comparetool_quarantine_{token}_{'f' * 32}"
            )
            os.replace(state["target"], legacy_quarantine)
            os.replace(state["backup"], state["target"])

            FileExporter.recover_transactions(root, raise_on_error=True)
            self.assertFalse(os.path.lexists(legacy_quarantine))
            self.assertFalse(os.path.exists(journal))
            with open(state["target"], "rb") as stream:
                self.assertEqual(b"old-data", stream.read())


if __name__ == "__main__":
    unittest.main()
