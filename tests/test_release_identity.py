import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
import app_version
from task_progress import select_log_dir
from tools.build_release import next_release_version, source_fingerprint


class ReleaseIdentityTests(unittest.TestCase):
    def test_source_mode_does_not_claim_to_be_packaged(self):
        with mock.patch.object(app_version.sys, "frozen", False, create=True):
            self.assertEqual("source", app_version.build_info()["build_id"])
            self.assertIn(app_version.APP_VERSION, app_version.window_title())

    def test_malformed_packaged_metadata_is_not_trusted(self):
        with mock.patch.object(app_version.sys, "frozen", True, create=True), mock.patch(
                "app_version.Path.read_text", return_value='{"version":"wrong"}'):
            self.assertEqual("unknown", app_version.build_info()["build_id"])

    def test_packaged_metadata_matches_window_identity(self):
        data = dict(version="2026.10.13.2", build_id="abc123", built_at="today",
                    commit="a"*40, source_sha256="b"*64)
        with mock.patch.object(app_version.sys, "frozen", True, create=True), mock.patch(
                "app_version.Path.read_text", return_value=json.dumps(data)):
            self.assertIn("abc123", app_version.window_title())
            self.assertIn("v2026.10.13.2", app_version.window_title())

    def test_invalid_release_date_and_sequence_are_not_trusted(self):
        for version in ("2026.02.30.1", "2026.10.13.0", "2026.10.13.01", None):
            with self.subTest(version=version):
                data = dict(version=version, build_id="abc123", built_at="today",
                            commit="a"*40, source_sha256="b"*64)
                with mock.patch.object(app_version.sys, "frozen", True, create=True), mock.patch(
                        "app_version.Path.read_text", return_value=json.dumps(data)):
                    self.assertEqual("unknown", app_version.build_info()["version"])
                    self.assertNotIn("v2026", app_version.window_title())

    def test_build_version_advances_on_same_day_and_resets_next_day(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            day = datetime(2026, 10, 13, 12)
            self.assertEqual("2026.10.13.1", next_release_version(output, day))
            (output / "CompareTool_build.json").write_text(
                json.dumps(dict(version="2026.10.13.1")), encoding="utf-8")
            self.assertEqual("2026.10.13.2", next_release_version(output, day))
            self.assertEqual("2026.10.14.1", next_release_version(output, day.replace(day=14)))

    def test_build_version_accounts_for_archives_after_an_old_release_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name, value in (("current", "2026.09.26.1"), ("same-day", "2026.10.13.4"),
                                ("future", "2026.10.14.8"), ("invalid", "wrong")):
                path = output if name == "current" else output / "archive" / name
                path.mkdir(parents=True, exist_ok=True)
                (path / "CompareTool_build.json").write_text(json.dumps(dict(version=value)), encoding="utf-8")
            self.assertEqual("2026.10.13.5", next_release_version(output, datetime(2026, 10, 13)))

    def test_fingerprint_changes_for_code_and_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("pass")
            for name in ("vcs", "templates", "assets"):
                (root / name).mkdir()
            initial = source_fingerprint(root)
            (root / "templates/report.html").write_text("changed")
            changed = source_fingerprint(root)
            self.assertNotEqual(initial, changed)
            (root / "notes.txt").write_text("irrelevant")
            self.assertEqual(changed, source_fingerprint(root))

    def test_diagnostics_never_written_into_compared_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            params = dict(vcs_type="folder", old_version=str(source), new_version=str(source))
            selected = select_log_dir(source / "logs/tasks", params)
            self.assertFalse(Path(selected).resolve().is_relative_to(source.resolve()))
            self.assertFalse(source.exists())

    @unittest.skipUnless(os.name == "nt", "Windows drive boundaries")
    def test_diagnostics_can_fall_back_to_a_different_drive(self):
        source = Path("D:/CompareTool_test_input")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/CompareTool_test_user"}):
            selected = select_log_dir(source / "logs/tasks", dict(
                vcs_type="folder", old_version=str(source), new_version=str(source)))
        self.assertEqual(Path("C:/CompareTool_test_user/CompareTool/logs/tasks"), selected)
        self.assertFalse(selected.is_relative_to(source))

    def test_no_logging_requested_remains_disabled(self):
        self.assertIsNone(select_log_dir(None, {}))


if __name__ == "__main__":
    unittest.main()
