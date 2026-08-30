import io
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from unittest import mock

from path_safety import open_regular_file_no_links
from vcs.archive_vcs import ArchiveVCS


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


def write_zip(path, payload, comment=b""):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("value.txt", payload)
        archive.comment = comment


def write_tar(path, payload, mode):
    with tarfile.open(path, mode) as archive:
        member = tarfile.TarInfo("value.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


def write_named_archive(path, member_name, payload):
    if path.endswith(".zip"):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(member_name, payload)
        return
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo(member_name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))


class ArchiveSourceCompatibilityTests(unittest.TestCase):
    def test_literal_short_looking_names_remain_valid_archive_members(self):
        for suffix in (".zip", ".tar"):
            with self.subTest(suffix=suffix), project_temp_dir() as root:
                old_archive = os.path.join(root, "old" + suffix)
                new_archive = os.path.join(root, "new" + suffix)
                member_name = "ABC~1/FILE~1.TXT"
                write_named_archive(old_archive, member_name, b"old")
                write_named_archive(new_archive, member_name, b"new")

                vcs = ArchiveVCS(old_archive, new_archive)
                try:
                    self.assertEqual(
                        [member_name],
                        [item.path for item in vcs.get_changed_files()],
                    )
                    self.assertEqual(
                        b"new",
                        vcs.get_file_content_bytes(new_archive, member_name),
                    )
                finally:
                    vcs.cleanup()

    def test_post_august_metadata_caps_are_disabled_by_default(self):
        self.assertIsNone(ArchiveVCS.MAX_TAR_METADATA_BYTES)
        self.assertIsNone(ArchiveVCS.MAX_TAR_METADATA_RECORDS)
        self.assertIsNone(ArchiveVCS.MAX_TAR_PAX_FIELDS)
        self.assertIsNone(ArchiveVCS.MAX_ZIP_CENTRAL_DIRECTORY_BYTES)
        ArchiveVCS._validate_zip_directory_limits(1, 300 * 1024 * 1024)

    def test_zip_central_directory_limit_remains_explicitly_configurable(self):
        with mock.patch.object(
            ArchiveVCS, "MAX_ZIP_CENTRAL_DIRECTORY_BYTES", 1
        ):
            with self.assertRaisesRegex(ValueError, "中央目录过大"):
                ArchiveVCS._validate_zip_directory_limits(1, 2)

    def test_tar_metadata_headers_are_not_capped_by_member_limit(self):
        payload = b"6 a=b\n"
        member = tarfile.TarInfo("pax")
        member.type = tarfile.XHDTYPE
        member.size = len(payload)
        record = member.tobuf(format=tarfile.PAX_FORMAT)
        record += payload + b"\0" * (512 - len(payload))
        archive = io.BytesIO(record * 1025 + b"\0" * 1024)

        # 真实成员数仍为 0；扩展元数据记录多不应偷偷套用由成员上限
        # 推导出来的另一道默认硬限制。
        with project_temp_dir() as root, mock.patch.object(
            ArchiveVCS, "MAX_ARCHIVE_MEMBERS", 0
        ):
            self.assertEqual(0, ArchiveVCS._preflight_tar(archive, root))

    def test_many_top_level_directories_do_not_rescan_all_siblings(self):
        with project_temp_dir() as root:
            real_scandir = os.scandir
            visited_entries = 0

            @contextmanager
            def counting_scandir(path):
                nonlocal visited_entries
                with real_scandir(path) as entries:
                    snapshot = list(entries)
                visited_entries += len(snapshot)
                yield iter(snapshot)

            cache = {}
            with mock.patch("vcs.archive_vcs.os.scandir", counting_scandir):
                for index in range(1000):
                    ArchiveVCS._ensure_archive_directory(
                        root, os.path.join(root, f"dir-{index}"), cache
                    )

            self.assertLess(visited_entries, 2000)

    def test_distinct_ntfs_names_do_not_collide_via_unicode_casefold(self):
        with project_temp_dir() as root:
            ArchiveVCS._validate_archive_targets(
                root,
                [("straße.txt", False), ("strasse.txt", False)],
            )

    def test_real_windows_case_collision_is_still_rejected(self):
        with project_temp_dir() as root:
            with self.assertRaisesRegex(ValueError, "同一 Windows 路径"):
                ArchiveVCS._validate_archive_targets(
                    root,
                    [("Demo.txt", False), ("demo.txt", False)],
                )

    def test_tar_preflight_and_extraction_use_the_bound_handle(self):
        for suffix, mode in (
            (".tar", "w"),
            (".tar.gz", "w:gz"),
            (".tar.bz2", "w:bz2"),
        ):
            with self.subTest(suffix=suffix), project_temp_dir() as root:
                old_tar = os.path.join(root, "old" + suffix)
                new_tar = os.path.join(root, "new" + suffix)
                write_tar(old_tar, b"old", mode)
                write_tar(new_tar, b"new", mode)

                vcs = ArchiveVCS(old_tar, new_tar)
                try:
                    self.assertEqual(
                        b"new",
                        vcs.get_file_content_bytes(new_tar, "value.txt"),
                    )
                finally:
                    vcs.cleanup()

    def test_large_compressed_sources_do_not_require_duplicate_source_space(self):
        with project_temp_dir() as root:
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            comment = b"x" * 60_000
            write_zip(old_zip, b"a", comment)
            write_zip(new_zip, b"b", comment)
            source_bytes = os.path.getsize(old_zip) + os.path.getsize(new_zip)
            self.assertGreater(source_bytes, 100_000)

            # 只有 8 字节“可用空间”足够展开两个 1 字节成员，但不足以再
            # 保存两份源归档。8 月 27 日后的源复制实现会在这里提前失败。
            usage = shutil.disk_usage(root)._replace(free=8)
            with mock.patch(
                "vcs.archive_vcs.shutil.disk_usage", return_value=usage
            ):
                vcs = ArchiveVCS(old_zip, new_zip)
            try:
                self.assertFalse(hasattr(vcs, "_tmp_archive_sources"))
                self.assertEqual(
                    ["value.txt"],
                    [item.path for item in vcs.get_changed_files("old", "new")],
                )
                self.assertEqual(
                    b"b", vcs.get_file_content_bytes(new_zip, "value.txt")
                )
            finally:
                vcs.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows deny-writes 句柄专属语义")
    def test_windows_locked_sources_skip_redundant_full_archive_hashes(self):
        with project_temp_dir() as root:
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            write_zip(old_zip, b"old")
            write_zip(new_zip, b"new")

            with mock.patch.object(
                ArchiveVCS,
                "_hash_archive_source",
                side_effect=AssertionError(
                    "deny-writes 句柄已锁定内容，不应再整包哈希"
                ),
            ):
                vcs = ArchiveVCS(old_zip, new_zip)
            try:
                self.assertEqual(
                    ["value.txt"],
                    [item.path for item in vcs.get_changed_files("old", "new")],
                )
            finally:
                vcs.cleanup()

    def test_source_change_during_direct_extraction_aborts_the_task(self):
        with project_temp_dir() as root:
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            write_zip(old_zip, b"old")
            write_zip(new_zip, b"new")
            real_extract = ArchiveVCS._extract
            changed = False

            denied = False

            def extract_then_change(vcs, source, dest, metadata):
                nonlocal changed
                nonlocal denied
                real_extract(vcs, source, dest, metadata)
                path = source.name
                if not changed and os.path.normcase(path) == os.path.normcase(old_zip):
                    changed = True
                    try:
                        write_zip(old_zip, b"changed")
                    except OSError:
                        denied = True

            with mock.patch.object(
                ArchiveVCS, "_extract", new=extract_then_change
            ):
                if os.name == "nt":
                    vcs = ArchiveVCS(old_zip, new_zip)
                    try:
                        self.assertTrue(denied)
                    finally:
                        vcs.cleanup()
                else:
                    with self.assertRaisesRegex(RuntimeError, "压缩包源.*发生变化"):
                        ArchiveVCS(old_zip, new_zip)

    def test_final_digest_detects_content_change_even_if_identity_looks_same(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "source.zip")
            with open(source, "wb") as stream:
                stream.write(b"A" * 4096)

            vcs = ArchiveVCS.__new__(ArchiveVCS)
            with open_regular_file_no_links(source) as held:
                capture = vcs._capture_archive_source(source, held)
                capture["digest"] = vcs._hash_archive_source(capture, held)
                with open(source, "r+b") as writer:
                    writer.write(b"B" * 4096)

                # 模拟大小、时间和稳定文件 ID 全部未暴露变化的情形，确认最终
                # 判定仍绑定真实内容，而不是只依赖廉价元数据。
                with mock.patch(
                    "vcs.archive_vcs.regular_file_path_identity",
                    return_value=capture["signature"],
                ), mock.patch(
                    "vcs.archive_vcs.regular_file_handle_identity",
                    return_value=capture["signature"],
                ):
                    with self.assertRaisesRegex(RuntimeError, "源内容.*发生变化"):
                        vcs._verify_archive_source(capture, held)

    def test_path_replacement_cannot_change_the_bound_extraction_endpoint(self):
        with project_temp_dir() as root:
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            replacement = os.path.join(root, "replacement.zip")
            held_path = os.path.join(root, "held-original.zip")
            write_zip(old_zip, b"original")
            write_zip(new_zip, b"new")
            write_zip(replacement, b"replacement")
            real_extract_zip = ArchiveVCS._extract_zip
            swapped = False
            denied = False

            def extract_with_path_aba(vcs, source, dest, metadata=None):
                nonlocal swapped, denied
                is_old = os.path.normcase(source.name) == os.path.normcase(old_zip)
                if is_old:
                    try:
                        os.replace(old_zip, held_path)
                        os.replace(replacement, old_zip)
                        swapped = True
                    except OSError:
                        denied = True
                try:
                    return real_extract_zip(vcs, source, dest, metadata)
                finally:
                    if swapped and is_old:
                        os.replace(old_zip, replacement)
                        os.replace(held_path, old_zip)

            with mock.patch.object(
                ArchiveVCS, "_extract_zip", new=extract_with_path_aba
            ):
                vcs = ArchiveVCS(old_zip, new_zip)
            try:
                if os.name == "nt":
                    self.assertTrue(denied)
                else:
                    self.assertTrue(swapped)
                self.assertEqual(
                    b"original",
                    vcs.get_file_content_bytes(old_zip, "value.txt"),
                )
            finally:
                vcs.cleanup()

    def test_explicit_source_size_policy_remains_available(self):
        with project_temp_dir() as root:
            old_zip = os.path.join(root, "old.zip")
            new_zip = os.path.join(root, "new.zip")
            write_zip(old_zip, b"old")
            write_zip(new_zip, b"new")

            with mock.patch.object(ArchiveVCS, "MAX_ARCHIVE_SOURCE_BYTES", 1):
                with self.assertRaisesRegex(RuntimeError, "源文件总大小超过上限"):
                    ArchiveVCS(old_zip, new_zip)


if __name__ == "__main__":
    unittest.main()
