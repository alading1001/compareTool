import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from vcs.archive_vcs import ArchiveVCS
from vcs.multi_version_vcs import _MultiVersionFolderDelegate
from vcs.temp_storage import (
    TEMP_DIR_ENV,
    candidate_temp_roots,
    create_temp_dir,
    open_temp_file,
)


def workspace_temp():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


class TempStorageTests(unittest.TestCase):
    def test_environment_override_controls_created_temp_directory(self):
        with workspace_temp() as root:
            configured = os.path.join(root, "configured")
            with mock.patch.dict(os.environ, {TEMP_DIR_ENV: configured}):
                created = create_temp_dir("override_")

            self.assertEqual(
                os.path.normcase(os.path.abspath(configured)),
                os.path.normcase(os.path.dirname(created)),
            )
            shutil.rmtree(created)

    def test_environment_override_controls_streaming_temp_file(self):
        with workspace_temp() as root:
            configured = os.path.join(root, "configured")
            with mock.patch.dict(os.environ, {TEMP_DIR_ENV: configured}):
                with open_temp_file("stream_") as stream:
                    stream.write(b"payload")
                    self.assertTrue(os.path.isdir(configured))

    @unittest.skipUnless(os.name == "nt", "Windows 盘符策略测试")
    def test_d_drive_precedes_system_temp_when_runtime_is_on_c(self):
        with mock.patch.dict(os.environ, {TEMP_DIR_ENV: ""}), \
                mock.patch("vcs.temp_storage._runtime_base_dir", return_value=r"C:\CompareTool"), \
                mock.patch("vcs.temp_storage._system_drive", return_value="c:"), \
                mock.patch("vcs.temp_storage.os.path.isdir", side_effect=lambda path: path == "D:\\"):
            roots = candidate_temp_roots()

        self.assertEqual(
            os.path.normcase(r"D:\applications\_cache\CompareTool\tmp"),
            os.path.normcase(roots[0]),
        )

    def test_archive_and_multi_version_workdirs_use_configured_root(self):
        with workspace_temp() as root:
            configured = os.path.join(root, "configured")
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            for path, content in ((old_zip, "old"), (new_zip, "new")):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("a.txt", content)

            with mock.patch.dict(os.environ, {TEMP_DIR_ENV: configured}):
                archive_vcs = ArchiveVCS(old_zip, new_zip)
                multi_vcs = _MultiVersionFolderDelegate("source", ["v1"], "multi_")
            try:
                expected = os.path.normcase(os.path.abspath(configured))
                self.assertEqual(expected, os.path.normcase(os.path.dirname(archive_vcs._tmp_old)))
                self.assertEqual(expected, os.path.normcase(os.path.dirname(archive_vcs._tmp_new)))
                self.assertEqual(expected, os.path.normcase(os.path.dirname(multi_vcs._tmp_root)))
            finally:
                archive_vcs.cleanup()
                multi_vcs.cleanup()


if __name__ == "__main__":
    unittest.main()
