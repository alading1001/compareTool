import itertools
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from vcs.multi_version_vcs import (
    GitMultiVersionVCS,
    SVNMultiVersionVCS,
    _EndpointPlanner,
    _GitAmbiguousPathCandidates,
    _HistoryChange,
    _LogicalFile,
    _MultiVersionFolderDelegate,
    _SVNPathChange,
    _longest_ancestor_candidate,
    _minimal_strict_descendant_paths,
    _path_is_covered_by_prefix,
    _strict_descendant_paths,
)
from vcs.git_vcs import GitVCS
from vcs.svn_vcs import SVNVCS


class PathPrefixIndexCompatibilityTests(unittest.TestCase):
    def test_prefix_queries_scale_by_path_depth_not_prefix_cartesian_product(self):
        count = 20_000
        prefixes = {f"old/{index}" for index in range(count)}

        for index in range(count):
            self.assertTrue(_path_is_covered_by_prefix(
                f"old/{index}/child/file.txt", prefixes
            ))

    def test_sorted_descendant_lookup_keeps_only_strict_subtree(self):
        paths = sorted([
            "a", "a/b", "a/b/c", "a2/b", "z",
        ])
        self.assertEqual(["a/b", "a/b/c"], _strict_descendant_paths(paths, "a"))

    def test_longest_ancestor_candidate_uses_most_specific_move(self):
        candidates = {
            "old": [(4, "old/", "new/")],
            "old/nested": [(11, "old/nested/", "new/deep/")],
        }
        self.assertEqual(
            (11, "old/nested/", "new/deep/"),
            _longest_ancestor_candidate("old/nested/file.txt", candidates),
        )

    def test_nested_move_prefixes_keep_only_topmost_descendants(self):
        paths = ["root/child", "root/child-sibling", "root/child.sibling"] + [
            "root/child/" + "/".join(f"level-{part}" for part in range(depth))
            for depth in range(1, 1000)
        ]

        self.assertEqual(
            ["root/child", "root/child-sibling", "root/child.sibling"],
            _minimal_strict_descendant_paths(sorted(paths), "root"),
        )


class DisabledBudgetCompatibilityTests(unittest.TestCase):
    def test_planner_does_not_scan_all_active_files_per_history_step(self):
        class NoFullScanDict(dict):
            def items(self):
                raise AssertionError("active file table must not be copied")

        planner = _EndpointPlanner()
        planner.apply([_HistoryChange("A", "a.txt")], False, "0", "1")
        planner._active = NoFullScanDict(planner._active)

        planner.apply([_HistoryChange("M", "a.txt")], False, "1", "2")
        planner.apply(
            [_HistoryChange("R", "b.txt", "a.txt")], True, "2", "3"
        )

        entity = planner.selected_entities[0]
        self.assertEqual("a.txt", entity.old_path)
        self.assertEqual("b.txt", entity.new_path)

    def test_planner_keeps_step_start_absence_across_repeated_touches(self):
        cases = (
            (
                [_HistoryChange("A", "x.txt"), _HistoryChange("D", "x.txt")],
                None,
            ),
            (
                [_HistoryChange("A", "x.txt"), _HistoryChange("M", "x.txt")],
                "x.txt",
            ),
            (
                [
                    _HistoryChange("A", "x.txt"),
                    _HistoryChange("R", "y.txt", "x.txt"),
                ],
                "y.txt",
            ),
        )
        for changes, expected_new_path in cases:
            with self.subTest(changes=changes):
                planner = _EndpointPlanner()
                planner.apply(changes, True, "old", "new")
                entity = planner.selected_entities[0]
                self.assertIsNone(entity.old_path)
                self.assertEqual(expected_new_path, entity.new_path)

    def test_default_git_prepare_skips_full_history_count_command(self):
        vcs = GitMultiVersionVCS.__new__(GitMultiVersionVCS)
        vcs._tmp_root = ""
        vcs.selected_versions = ["selected"]
        vcs._content_vcs = SimpleNamespace(
            _snapshot_git_config=mock.Mock()
        )
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args == ("rev-list", "--first-parent", "HEAD"):
                return "selected\nbase"
            if args[:2] == ("rev-parse", "--verify"):
                return "selected"
            raise AssertionError(f"unexpected git command: {args}")

        vcs._git = fake_git
        vcs._first_parent = mock.Mock(return_value="base")
        vcs._changes_for_commit = mock.Mock(return_value=(
            [], _GitAmbiguousPathCandidates(set(), [])
        ))
        vcs._finish_plan = mock.Mock()

        vcs._prepare()

        self.assertNotIn(
            ("rev-list", "--first-parent", "--count", "HEAD"), calls
        )
        vcs._finish_plan.assert_called_once()

    def test_explicit_history_limit_still_uses_count_and_rejects(self):
        vcs = GitMultiVersionVCS.__new__(GitMultiVersionVCS)
        vcs._tmp_root = ""
        vcs.selected_versions = ["selected"]
        vcs._content_vcs = SimpleNamespace(
            _snapshot_git_config=mock.Mock()
        )
        vcs._git = mock.Mock(return_value="2")

        with mock.patch.object(_EndpointPlanner, "MAX_HISTORY_STEPS", 1):
            with self.assertRaisesRegex(RuntimeError, "历史提交数超过"):
                vcs._prepare()

        vcs._git.assert_called_once_with(
            "rev-list", "--first-parent", "--count", "HEAD"
        )

    def test_default_planner_does_not_encode_paths_for_disabled_budget(self):
        class NoBudgetEncode(str):
            def encode(self, *args, **kwargs):
                raise AssertionError("disabled path budget must not encode paths")

        planner = _EndpointPlanner()
        planner.apply(
            [_HistoryChange("A", NoBudgetEncode("a.txt"))],
            selected=False,
            old_version="old",
            new_version="new",
        )

        self.assertEqual(0, planner._change_count)
        self.assertEqual(0, planner._path_bytes)

    def test_default_endpoint_budget_skips_size_and_disk_preflight(self):
        delegate = _MultiVersionFolderDelegate.__new__(
            _MultiVersionFolderDelegate
        )
        delegate._content_vcs = SimpleNamespace(
            get_file_size=mock.Mock(
                side_effect=AssertionError("size probe must be skipped")
            )
        )
        entity = _LogicalFile(
            1,
            old_version="1",
            old_path="old.txt",
            new_version="2",
            new_path="new.txt",
        )

        with mock.patch(
            "vcs.multi_version_vcs.shutil.disk_usage",
            side_effect=AssertionError("disk preflight must be skipped"),
        ):
            delegate._reserve_endpoint_budget(
                [(entity, entity.old_path, entity.new_path)]
            )

        delegate._content_vcs.get_file_size.assert_not_called()

    def test_default_post_write_skips_size_and_free_space_queries(self):
        delegate = _MultiVersionFolderDelegate.__new__(
            _MultiVersionFolderDelegate
        )
        delegate._endpoint_written_bytes = 0
        with tempfile.TemporaryDirectory() as root:
            delegate._tmp_root = root
            target = os.path.join(root, "endpoint.txt")

            def writer(_version, _path, output):
                with open(output, "wb") as stream:
                    stream.write(b"payload")

            with mock.patch(
                "vcs.multi_version_vcs.os.path.getsize",
                side_effect=AssertionError("getsize must be skipped"),
            ), mock.patch(
                "vcs.multi_version_vcs.shutil.disk_usage",
                side_effect=AssertionError("disk_usage must be skipped"),
            ):
                delegate._write_endpoint_file(
                    writer, "1", "endpoint.txt", target, "测试"
                )
            delegate._tmp_root = ""


class GitRenameCompatibilityTests(unittest.TestCase):
    def test_git_object_signature_does_not_repeat_size_query(self):
        vcs = GitVCS.__new__(GitVCS)
        vcs._resolve_version = mock.Mock(return_value="a" * 40)
        vcs.get_file_size = mock.Mock(
            side_effect=AssertionError("blob OID already binds the full content")
        )
        vcs._run_bytes = mock.Mock(return_value=("b" * 40).encode("ascii"))

        self.assertEqual(
            ("git-object", "b" * 40),
            vcs.get_file_signature("main", "demo.txt"),
        )
        vcs.get_file_size.assert_not_called()
        vcs._run_bytes.assert_called_once_with([
            "rev-parse", "--verify", f"{'a' * 40}:demo.txt"
        ])

    @staticmethod
    def _bare_git(root, content_vcs):
        vcs = GitMultiVersionVCS.__new__(GitMultiVersionVCS)
        vcs._tmp_root = root
        vcs.source_project_path = root
        vcs._git_exe = "git"
        vcs._content_vcs = content_vcs
        vcs._git_rename_candidate_cache = {}
        vcs._git_pair_candidates = 0
        vcs._git_scoring_evaluations = 0
        vcs._git_scoring_bytes = 0
        vcs._git_stored_ambiguous_candidates = 0
        vcs.exclude_patterns = []
        return vcs

    def test_default_rename_scoring_skips_blob_size_queries(self):
        class ContentVCS:
            @staticmethod
            def get_file_size(_version, _path):
                raise AssertionError("disabled scoring budget must not query size")

            @staticmethod
            def export_raw_file_to_path(_version, _path, target):
                with open(target, "wb") as stream:
                    stream.write(b"same")

        with tempfile.TemporaryDirectory() as root:
            vcs = self._bare_git(root, ContentVCS())
            completed = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"R100\x00source\x00target\x00", stderr=b""
            )
            with mock.patch(
                "vcs.multi_version_vcs.subprocess.run", return_value=completed
            ):
                self.assertTrue(
                    vcs._git_rename_candidate("old", "a", "new", "b")
                )
            self.assertEqual(0, vcs._git_scoring_evaluations)
            self.assertEqual(0, vcs._git_scoring_bytes)
            vcs._tmp_root = ""

    def test_explicit_scoring_byte_limit_still_queries_and_rejects(self):
        content = SimpleNamespace(get_file_size=mock.Mock(return_value=6))
        with tempfile.TemporaryDirectory() as root:
            vcs = self._bare_git(root, content)
            vcs.MAX_GIT_RENAME_SCORING_BYTES = 10
            with self.assertRaisesRegex(RuntimeError, "评分累计字节数"):
                vcs._git_rename_candidate("old", "a", "new", "b")
            self.assertEqual(2, content.get_file_size.call_count)
            vcs._tmp_root = ""

    def test_delete_add_cartesian_pairs_are_iterated_lazily(self):
        with tempfile.TemporaryDirectory() as root:
            vcs = self._bare_git(root, SimpleNamespace())
            changes = [
                *(_HistoryChange("D", f"old/{index}.txt") for index in range(500)),
                *(_HistoryChange("A", f"new/{index}.txt") for index in range(500)),
            ]
            vcs._diff_changes = mock.Mock(return_value=changes)

            reliable, pairs = vcs._changes_for_commit("new", "old")

            self.assertEqual(len(changes), len(reliable))
            self.assertEqual(0, vcs._git_pair_candidates)
            self.assertNotIsInstance(pairs, (list, set, tuple))
            self.assertEqual(
                [
                    ("old/0.txt", "new/0.txt"),
                    ("old/0.txt", "new/1.txt"),
                    ("old/0.txt", "new/2.txt"),
                ],
                list(itertools.islice(pairs, 3)),
            )
            vcs._diff_changes.assert_called_once_with("new", "old", "50%")
            vcs._tmp_root = ""

    def test_permissive_diff_is_deferred_until_candidates_cross_selected_endpoints(self):
        with tempfile.TemporaryDirectory() as root:
            vcs = self._bare_git(root, SimpleNamespace())
            source = _LogicalFile(1, selected=True)
            target = _LogicalFile(2, last_selected_step=4)
            groups = [([("old.txt", source, True)], [("new.txt", target)])]
            vcs._diff_changes = mock.Mock(
                return_value=[_HistoryChange("R", "new.txt", "old.txt")]
            )

            self.assertIsNone(vcs._permissive_rename_witness(
                "commit", "parent", 4, groups, set()
            ))
            vcs._diff_changes.assert_not_called()

            target.last_selected_step = 5
            self.assertEqual(
                ("old.txt", "new.txt"),
                vcs._permissive_rename_witness(
                    "commit", "parent", 4, groups, set()
                ),
            )
            vcs._diff_changes.assert_called_once_with(
                "commit", "parent", "1%"
            )
            vcs._tmp_root = ""

    def test_same_entity_candidates_do_not_run_permissive_diff_or_blob_scoring(self):
        with tempfile.TemporaryDirectory() as root:
            vcs = self._bare_git(root, SimpleNamespace())
            entity = _LogicalFile(1, selected=True, last_selected_step=5)
            groups = [([("old.txt", entity, True)], [("new.txt", entity)])]
            vcs._diff_changes = mock.Mock()
            vcs._git_rename_candidate = mock.Mock()

            self.assertIsNone(vcs._permissive_rename_witness(
                "commit", "parent", 4, groups, set()
            ))
            self.assertIsNone(vcs._scored_rename_witness(
                "commit", "parent", 4, groups
            ))
            vcs._diff_changes.assert_not_called()
            vcs._git_rename_candidate.assert_not_called()
            vcs._tmp_root = ""

    def test_scoring_stops_after_first_relevant_positive_witness(self):
        with tempfile.TemporaryDirectory() as root:
            vcs = self._bare_git(root, SimpleNamespace())
            source = _LogicalFile(1, selected=True)
            target = _LogicalFile(2, last_selected_step=5)
            extra = _LogicalFile(3, last_selected_step=5)
            groups = [
                (
                    [("old.txt", source, True)],
                    [("new.txt", target), ("extra.txt", extra)],
                )
            ]
            vcs._git_rename_candidate = mock.Mock(return_value=True)

            self.assertEqual(
                ("old.txt", "new.txt"),
                vcs._scored_rename_witness(
                    "commit", "parent", 4, groups
                ),
            )
            vcs._git_rename_candidate.assert_called_once_with(
                "parent", "old.txt", "commit", "new.txt"
            )
            vcs._tmp_root = ""

    def test_pure_rename_commit_does_not_run_unused_one_percent_diff(self):
        with tempfile.TemporaryDirectory() as root:
            vcs = self._bare_git(root, SimpleNamespace())
            changes = [
                _HistoryChange("R", "b.txt", "a.txt"),
                _HistoryChange("R", "d.txt", "c.txt"),
            ]
            vcs._diff_changes = mock.Mock(return_value=changes)

            _reliable, candidates = vcs._changes_for_commit("new", "old")

            vcs._diff_changes.assert_called_once_with("new", "old", "50%")
            self.assertFalse(candidates.permissive_groups)
            vcs._tmp_root = ""


class SVNCompatibilityTests(unittest.TestCase):
    def test_svn_summary_url_uses_case_sensitive_longest_project_root(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._project_url_at = mock.Mock(side_effect=lambda revision: {
            "1": "https://example.invalid/repo/A",
            "2": "https://example.invalid/repo/a/sub",
        }[revision])

        self.assertEqual(
            "file.txt",
            vcs._summary_relative_path(
                "https://example.invalid/repo/a/sub/file.txt", "1", "2"
            ),
        )

    def test_repos_size_failure_is_unknown_not_generation_failure(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs.project_path = "demo"
        vcs._svn = "svn"
        vcs._resolve_version = mock.Mock(return_value="12")
        vcs._file_url = mock.Mock(return_value="https://example.invalid/a.txt@12")
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"unsupported show-item"
        )

        with mock.patch("vcs.svn_vcs.subprocess.run", return_value=failed):
            self.assertIsNone(vcs.get_file_size("12", "a.txt"))

    def test_normal_svn_records_informational_property_change(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._get_properties = mock.Mock(side_effect=[
            {"custom:deployment": "old"},
            {"custom:deployment": "new"},
        ])

        metadata = vcs._compare_endpoint_metadata(
            "1", "demo.txt", "2", "demo.txt"
        )

        self.assertIn("custom:deployment", "\n".join(metadata["metadata_changes"]))

    def test_multi_svn_records_informational_property_change(self):
        vcs = SVNMultiVersionVCS.__new__(SVNMultiVersionVCS)
        vcs._validate_svn_regular_endpoint = mock.Mock(side_effect=[
            {"svn:mime-type": "text/plain"},
            {"svn:mime-type": "application/json"},
        ])

        metadata = vcs._compare_endpoint_metadata(
            "1", "demo.txt", "2", "demo.txt"
        )

        self.assertIn("svn:mime-type", "\n".join(metadata["changes"]))

    def test_normal_svn_directory_custom_property_warns_and_continues(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._pinned_project_url = "https://example.invalid/project"
        vcs._pinned_peg_revision = "2"
        vcs.exclude_patterns = []
        vcs._run = mock.Mock(return_value=(
            '<?xml version="1.0"?><diff><paths>'
            '<path item="none" props="modified" kind="dir">config</path>'
            '</paths></diff>'
        ))
        vcs._summary_relative_path = mock.Mock(return_value="config")
        vcs._get_properties = mock.Mock(side_effect=[
            {"custom:deployment": "old"},
            {"custom:deployment": "new"},
        ])

        with mock.patch("vcs.svn_vcs.warn") as warning:
            self.assertEqual([], vcs._parse_svn_diff_summarize("1", "2"))

        self.assertIn("custom:deployment", warning.call_args.args[0])

    def test_multi_svn_directory_custom_property_warns_and_continues(self):
        vcs = SVNMultiVersionVCS.__new__(SVNMultiVersionVCS)
        vcs.exclude_patterns = []
        vcs.required_directory_deletions = []
        vcs._get_svn_properties = mock.Mock(side_effect=[
            {"custom:deployment": "old"},
            {"custom:deployment": "new"},
        ])
        planner = _EndpointPlanner()

        with mock.patch("vcs.multi_version_vcs.warn") as warning:
            changes = vcs._expand_svn_changes(
                [_SVNPathChange(
                    "M", "dir", "config", props_modified=True
                )],
                revision=2,
                planner=planner,
                selected=True,
            )

        self.assertEqual([], changes)
        self.assertIn("custom:deployment", warning.call_args.args[0])

    def test_normal_svn_property_only_directory_externals_aborts(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._pinned_project_url = "https://example.invalid/project"
        vcs._pinned_peg_revision = "2"
        vcs.exclude_patterns = []
        vcs._run = mock.Mock(return_value=(
            '<?xml version="1.0"?><diff><paths>'
            '<path item="none" props="modified" kind="dir">config</path>'
            '</paths></diff>'
        ))
        vcs._summary_relative_path = mock.Mock(return_value="config")
        vcs._get_properties = mock.Mock(side_effect=[
            {}, {"svn:externals": "vendor https://example.invalid/vendor"},
        ])

        with self.assertRaisesRegex(RuntimeError, "svn:externals"):
            vcs._parse_svn_diff_summarize("1", "2")

    def test_normal_svn_root_externals_change_aborts(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._pinned_project_url = "https://example.invalid/project"
        vcs._pinned_peg_revision = "2"
        vcs.exclude_patterns = []
        vcs._run = mock.Mock(return_value=(
            '<?xml version="1.0"?><diff><paths>'
            '<path item="none" props="modified" kind="dir">root</path>'
            '</paths></diff>'
        ))
        vcs._summary_relative_path = mock.Mock(return_value="")
        vcs._get_properties = mock.Mock(side_effect=[
            {"svn:externals": "old https://example.invalid/old"}, {},
        ])

        with self.assertRaisesRegex(RuntimeError, "项目根"):
            vcs._parse_svn_diff_summarize("1", "2")

    def test_special_and_keywords_remain_fail_closed(self):
        for property_name in ("svn:special", "svn:keywords"):
            with self.subTest(property_name=property_name):
                vcs = SVNVCS.__new__(SVNVCS)
                vcs._get_properties = mock.Mock(side_effect=[
                    {}, {property_name: "*"},
                ])
                with self.assertRaises(RuntimeError):
                    vcs._compare_endpoint_metadata(
                        "1", "demo.txt", "2", "demo.txt"
                    )


if __name__ == "__main__":
    unittest.main()
