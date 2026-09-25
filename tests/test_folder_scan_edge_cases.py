"""Regression coverage for scan metadata reuse and case-sensitive roots."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import path_safety
from vcs.folder_vcs import FolderVCS


class FolderScanEdgeCases(unittest.TestCase):
    def test_case_distinct_constructor_roots_keep_two_cache_entries(self):
        lower = os.path.abspath('case_fixture/old')
        upper = os.path.abspath('case_fixture/OLD')
        with mock.patch('vcs.folder_vcs.os.path.realpath', side_effect=os.path.abspath), \
                mock.patch('vcs.folder_vcs.os.path.samefile', return_value=False):
            vcs = FolderVCS(lower, upper)
        try:
            self.assertEqual(2, len(vcs._real_root_cache))
            self.assertEqual(lower, vcs._real_root_cache[lower])
            self.assertEqual(upper, vcs._real_root_cache[upper])
        finally:
            vcs.cleanup()

    def test_case_distinct_resolved_roots_keep_two_cache_entries(self):
        vcs = FolderVCS.__new__(FolderVCS)
        vcs._owned_temp_dirs = []
        vcs._real_root_cache = {}
        lower = os.path.abspath('case_fixture/old')
        upper = os.path.abspath('case_fixture/OLD')
        with mock.patch('vcs.folder_vcs.os.path.realpath', side_effect=os.path.abspath):
            for root in (lower, upper):
                vcs._resolve_file_path(root, 'a.txt', check_leaf_link=False)
        self.assertEqual(2, len(vcs._real_root_cache))
        self.assertEqual(lower, vcs._real_root_cache[lower])
        self.assertEqual(upper, vcs._real_root_cache[upper])

    def test_symlink_mode_is_recognized_without_path_query(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFLNK, st_reparse_tag=0)
        self.assertTrue(path_safety.metadata_is_link_or_junction(metadata))

    def test_non_redirecting_reparse_tag_is_not_treated_as_a_link(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFREG, st_reparse_tag=0x9000001A)
        self.assertFalse(path_safety.metadata_is_link_or_junction(metadata))

    def test_missing_path_is_not_a_link(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(path_safety.is_link_or_junction(str(Path(root) / 'missing')))

    def test_path_identity_is_refreshed_on_every_open(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'data.txt'
            path.write_bytes(b'first')
            before = path_safety.regular_file_path_identity(str(path))
            path.write_bytes(b'changed-and-longer')
            after = path_safety.regular_file_path_identity(str(path))
            self.assertNotEqual(before, after)

    def test_handle_identity_after_read_is_not_the_cached_initial_value(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'data.txt'
            path.write_bytes(b'first')
            with path_safety.open_regular_file_no_links(str(path)) as stream:
                before = path_safety.regular_file_handle_identity(stream)
                path.write_bytes(b'changed-and-longer')
                after = path_safety.regular_file_handle_identity(stream)
                self.assertNotEqual(before, after)
                self.assertEqual(before, stream._initial_identity)
