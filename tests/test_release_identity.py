import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import app_version
from task_progress import select_log_dir
from tools.build_release import source_fingerprint


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
        data = dict(version=app_version.APP_VERSION, build_id="abc123", built_at="today",
                    commit="a"*40, source_sha256="b"*64)
        with mock.patch.object(app_version.sys, "frozen", True, create=True), mock.patch(
                "app_version.Path.read_text", return_value=json.dumps(data)):
            self.assertIn("abc123", app_version.window_title())

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
