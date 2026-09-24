"""Interrupted output retries must preserve evidence and stop blocking once recoverable."""
import json
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from file_exporter import FileExporter
from main import CompareToolApp
from path_safety import open_regular_file_no_links
from stage_ownership import is_owned


class OutputRecoveryRetryTests(unittest.TestCase):
    def setUp(self):
        temp_base = Path(__file__).resolve().parents[1] / ".tmp"
        temp_base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_base, prefix="recovery_retry_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {
            FileExporter.TRANSACTION_KEY_ENV: str(self.root / "private.key"),
            "COMPARETOOL_TEMP_DIR": str(self.root / "temp"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.warn = mock.patch("file_exporter.warn")
        self.warn.start()
        self.addCleanup(self.warn.stop)

    def pending(self, name="Demo_diff.html", discard_stage=False):
        target = self.root / "batch" / name
        target.parent.mkdir(exist_ok=True)
        stage = self.root / ".comparetool_report_pending1.html"
        stage.write_bytes(b"new report")
        target.write_bytes(b"old report")
        token = "a" * 32
        state = dict(stage=str(stage), target=str(target),
                     backup=f"{target}.comparetool_backup_{token}", had_target=True)
        journal = FileExporter._create_transaction_journal([state], token, root=str(self.root))
        FileExporter._mark_transaction(journal, "rollback")
        os.replace(target, state["backup"])
        if discard_stage:
            stage.unlink()
        return state, Path(journal)

    def test_missing_stage_restores_verified_backup_idempotently(self):
        state, journal = self.pending(discard_stage=True)
        FileExporter.recover_transactions(str(self.root), raise_on_error=True)
        self.assertEqual(Path(state["target"]).read_bytes(), b"old report")
        self.assertFalse(journal.exists())
        self.assertEqual(FileExporter.recover_transactions(str(self.root), raise_on_error=True), [])

    def test_finally_keeps_stage_and_owner_until_journal_is_removed(self):
        state, journal = self.pending()
        FileExporter.cleanup_stages([(state["stage"], state["target"])])
        self.assertEqual(Path(state["stage"]).read_bytes(), b"new report")
        self.assertTrue(is_owned(state["stage"]))
        FileExporter.recover_transactions(str(self.root), raise_on_error=True)
        FileExporter.cleanup_stages([(state["stage"], state["target"])])
        self.assertFalse(journal.exists())
        self.assertFalse(Path(state["stage"]).exists())

    def test_changed_backup_is_preserved_and_rejected(self):
        state, journal = self.pending(discard_stage=True)
        Path(state["backup"]).write_bytes(b"another writer")
        with self.assertRaisesRegex(RuntimeError, "身份|元数据"):
            FileExporter.recover_transactions(str(self.root), raise_on_error=True)
        self.assertTrue(journal.exists())
        self.assertEqual(Path(state["backup"]).read_bytes(), b"another writer")

    def test_existing_unrecognized_target_is_never_overwritten(self):
        state, journal = self.pending(discard_stage=True)
        Path(state["target"]).write_bytes(b"another writer")
        with self.assertRaisesRegex(RuntimeError, "身份|元数据"):
            FileExporter.recover_transactions(str(self.root), raise_on_error=True)
        self.assertTrue(journal.exists())
        self.assertEqual(Path(state["target"]).read_bytes(), b"another writer")

    def test_committed_transaction_cannot_restore_old_backup_as_success(self):
        state, journal = self.pending(discard_stage=True)
        Path(f"{journal}.rollback").unlink()
        FileExporter._mark_transaction(str(journal), "commit")
        with self.assertRaisesRegex(RuntimeError, "未完整安装"):
            FileExporter.recover_transactions(str(self.root), raise_on_error=True)
        self.assertTrue(journal.exists())
        self.assertFalse(Path(state["target"]).exists())

    def test_locked_journal_keeps_decision_and_ownership(self):
        state, journal = self.pending()
        real_remove = os.remove

        def remove(path):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(journal)):
                raise PermissionError("journal locked")
            return real_remove(path)

        with mock.patch("file_exporter.os.remove", side_effect=remove):
            FileExporter._remove_journal(str(journal))
        self.assertTrue(journal.exists())
        self.assertTrue(is_owned(str(journal)))
        self.assertTrue(Path(f"{journal}.rollback").exists())
        FileExporter.recover_transactions(str(self.root), raise_on_error=True)
        self.assertFalse(journal.exists())

    def test_unrelated_generation_does_not_touch_pending_transaction(self):
        state, journal = self.pending(discard_stage=True)
        Path(state["backup"]).write_bytes(b"another writer")
        journal_bytes = journal.read_bytes()
        target = self.root / "batch" / "Other_diff.html"
        expected = FileExporter.prepare_target_states([str(target)], trusted_root=str(self.root))
        stage = self.root / ".comparetool_report_other123.html"
        stage.write_bytes(b"other output")
        FileExporter._replace_outputs([(str(stage), str(target))],
                                      expected_target_states=expected, trusted_root=str(self.root))
        self.assertEqual(target.read_bytes(), b"other output")
        self.assertEqual(journal.read_bytes(), journal_bytes)
        self.assertEqual(Path(state["backup"]).read_bytes(), b"another writer")

    def test_overlapping_target_and_parent_still_block(self):
        state, journal = self.pending(discard_stage=True)
        Path(state["backup"]).write_bytes(b"another writer")
        for target in (state["target"], str(Path(state["target"]).parent), state["target"].upper(),
                       state["backup"], state["stage"]):
            with self.subTest(target=target), self.assertRaisesRegex(RuntimeError, "身份|元数据"):
                FileExporter.prepare_target_states([target], trusted_root=str(self.root))
        self.assertTrue(journal.exists())

    def test_untrusted_signature_cannot_claim_to_be_unrelated(self):
        state, journal = self.pending(discard_stage=True)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload["hmac_sha256"] = "0" * 64
        journal.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "HMAC"):
            FileExporter.prepare_target_states([str(self.root / "Other.html")], trusted_root=str(self.root))
        self.assertTrue(Path(state["backup"]).exists())

    def test_gui_first_retry_recovers_before_capturing_generation_baseline(self):
        state, journal = self.pending(discard_stage=True)
        source_old, source_new = self.root / "source-old", self.root / "source-new"
        source_old.mkdir()
        source_new.mkdir()
        (source_old / "value.txt").write_bytes(b"old")
        (source_new / "value.txt").write_bytes(b"new")
        app = CompareToolApp.__new__(CompareToolApp)
        app.root = mock.Mock()
        app.root.after.return_value = None
        with mock.patch("main.error"):
            app._do_generate(str(source_new), "folder", str(source_old), str(source_new),
                             "Demo", [], True, True, state["target"],
                             str(self.root / "batch" / "oldVersion"),
                             str(self.root / "batch" / "newVersion"),
                             trusted_output_root=str(self.root))
        self.assertFalse(journal.exists())
        self.assertIn(b"<!DOCTYPE html>", Path(state["target"]).read_bytes())
        self.assertEqual((self.root / "batch/newVersion/Demo/value.txt").read_bytes(), b"new")

    @unittest.skipUnless(os.name == "nt", "Windows sharing handles")
    def test_real_windows_locks_release_then_retry_succeeds(self):
        target = self.root / "report.html"
        target.write_bytes(b"old report")
        stage = self.root / ".comparetool_report_locked123.html"
        stage.write_bytes(b"new report")
        real_replace = os.replace
        with ExitStack() as locks:
            locks.enter_context(open_regular_file_no_links(str(stage), deny_writes=True))

            def replace(source, destination):
                real_replace(source, destination)
                if ".comparetool_backup_" in str(destination):
                    locks.enter_context(open_regular_file_no_links(str(destination), deny_writes=True))

            with mock.patch("file_exporter.os.replace", side_effect=replace):
                with self.assertRaisesRegex(RuntimeError, "32"):
                    FileExporter._replace_outputs([(str(stage), str(target))], trusted_root=str(self.root))
            FileExporter.cleanup_stages([(str(stage), str(target))])
            self.assertTrue(stage.exists())
            self.assertTrue(list(self.root.glob(".comparetool_transaction_*.json")))

        expected = FileExporter.prepare_target_states([str(target)], trusted_root=str(self.root))
        self.assertEqual(target.read_bytes(), b"old report")
        stage.write_bytes(b"retried report")
        FileExporter._replace_outputs([(str(stage), str(target))],
                                      expected_target_states=expected, trusted_root=str(self.root))
        self.assertEqual(target.read_bytes(), b"retried report")
        self.assertFalse(list(self.root.glob(".comparetool_transaction_*.json")))


if __name__ == "__main__":
    unittest.main()
