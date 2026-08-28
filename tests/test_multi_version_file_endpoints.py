import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from delivery_instructions import (
    DELIVERY_INSTRUCTIONS_FILENAME,
    prepare_delivery_instructions,
    write_delivery_instructions,
)
from diff_engine import DiffEngine, DiffResult, FileDiff
from file_exporter import FileExporter
from vcs.base import ChangeType
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS, SVNMultiVersionVCS
from vcs.svn_vcs import SVNVCS


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


def run(command, cwd, check=True):
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed: {command}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def normalized(data):
    return data.decode("utf-8").replace("\r\n", "\n")


class GitFileEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = project_temp_dir()
        self.repo = self.temp.name
        self.git = GitVCS._find_git()
        run([self.git, "init", "-b", "main"], self.repo)
        run([self.git, "config", "user.name", "CompareTool Test"], self.repo)
        run([self.git, "config", "user.email", "comparetool@example.invalid"], self.repo)
        run([self.git, "config", "core.autocrlf", "false"], self.repo)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, content):
        target = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(target) or self.repo, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    def write_bytes(self, path, content):
        target = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(target) or self.repo, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(content)

    def commit(self, message):
        run([self.git, "add", "-A"], self.repo)
        run([self.git, "commit", "-m", message], self.repo)
        return run([self.git, "rev-parse", "HEAD"], self.repo).stdout.strip()

    def stage_symlink(self, path, target):
        payload = os.path.join(self.repo, ".comparetool-symlink-payload")
        with open(payload, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(target)
        blob = run([self.git, "hash-object", "-w", payload], self.repo).stdout.strip()
        os.remove(payload)
        run(
            [self.git, "update-index", "--cacheinfo", f"120000,{blob},{path}"],
            self.repo,
        )

    def test_normal_and_multi_git_preserve_mode_only_change(self):
        self.write("script.sh", "echo ok\n")
        baseline = self.commit("baseline")
        run([self.git, "update-index", "--chmod=+x", "script.sh"], self.repo)
        run([self.git, "commit", "-m", "make executable"], self.repo)
        selected = run([self.git, "rev-parse", "HEAD"], self.repo).stdout.strip()

        normal_files = GitVCS(self.repo).get_changed_files(baseline, selected)
        self.assertEqual(1, len(normal_files))
        self.assertEqual(["Git 文件模式：100644 → 100755"], normal_files[0].metadata_changes)
        self.assertTrue(normal_files[0].new_executable)

        vcs = GitMultiVersionVCS(self.repo, [selected])
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.MODIFIED, files[0].change_type)
            self.assertEqual(["Git 文件模式：100644 → 100755"], files[0].metadata_changes)
            diff = DiffEngine(vcs).generate_diff("ui-selected", vcs.new_version_label)
            self.assertIn("文件元数据变化", diff.files[0].side_by_side_html)
        finally:
            vcs.cleanup()

    def test_each_file_uses_its_own_selected_endpoints_not_head(self):
        self.write("A.java", "A-base\n")
        self.write("B.java", "B-base\n")
        self.commit("baseline")

        self.write("B.java", "B-selected-r2\n")
        selected_b = self.commit("selected B")
        self.write("A.java", "A-unselected-r3\n")
        self.commit("unselected A")
        self.write("A.java", "A-selected-r4\n")
        selected_a = self.commit("selected A")
        self.write("B.java", "B-later-head\n")
        self.commit("later B")

        vcs = GitMultiVersionVCS(self.repo, [selected_b, selected_a])
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual({"A.java": ChangeType.MODIFIED, "B.java": ChangeType.MODIFIED}, files)
            self.assertEqual(
                "A-unselected-r3\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "A.java")),
            )
            self.assertEqual(
                "A-selected-r4\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "A.java")),
            )
            self.assertEqual(
                "B-base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "B.java")),
            )
            self.assertEqual(
                "B-selected-r2\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "B.java")),
            )

            # GUI 会用换行保存多选版本；报告和导出必须仍把它识别为 old，
            # 不能因其不同于内部逗号标签而误读 new 端点。
            raw_ui_versions = f"{selected_b}\n{selected_a}"
            diff = DiffEngine(vcs).generate_diff(
                raw_ui_versions, vcs.new_version_label
            )
            by_path = {item.file_path: item for item in diff.files}
            self.assertEqual(
                "B-base\n", by_path["B.java"].old_content.replace("\r\n", "\n")
            )
            self.assertEqual(
                "B-selected-r2\n",
                by_path["B.java"].new_content.replace("\r\n", "\n"),
            )
            with project_temp_dir() as export_root:
                old_dir = os.path.join(export_root, "old")
                new_dir = os.path.join(export_root, "new")
                FileExporter(diff, vcs).export(old_dir, new_dir)
                with open(os.path.join(old_dir, "B.java"), "rb") as handle:
                    self.assertEqual(b"B-base\n", handle.read())
                with open(os.path.join(new_dir, "B.java"), "rb") as handle:
                    self.assertEqual(b"B-selected-r2\n", handle.read())
        finally:
            vcs.cleanup()

    def test_selected_changes_that_cancel_out_are_omitted(self):
        self.write("A.java", "base\n")
        self.commit("baseline")
        self.write("A.java", "temporary\n")
        first = self.commit("selected temporary")
        self.write("A.java", "middle\n")
        self.commit("unselected middle")
        self.write("A.java", "base\n")
        last = self.commit("selected restore")

        vcs = GitMultiVersionVCS(self.repo, [first, last])
        try:
            self.assertEqual([], vcs.get_changed_files())
        finally:
            vcs.cleanup()

    def test_unselected_rename_between_selected_changes_keeps_one_logical_file(self):
        self.write("A.java", "base\n")
        self.commit("baseline")
        self.write("A.java", "selected-before-rename\n")
        first = self.commit("selected A")
        run([self.git, "mv", "A.java", "B.java"], self.repo)
        self.commit("unselected rename")
        self.write("B.java", "selected-after-rename\n")
        last = self.commit("selected B")

        vcs = GitMultiVersionVCS(self.repo, [first, last])
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("A.java", files[0].old_path)
            self.assertEqual("B.java", files[0].path)
            diff = DiffEngine(vcs).generate_diff(vcs.old_version_label, vcs.new_version_label)
            self.assertEqual(ChangeType.RENAMED, diff.files[0].change_type)
            self.assertEqual("base\n", diff.files[0].old_content.replace("\r\n", "\n"))
            self.assertEqual(
                "selected-after-rename\n",
                diff.files[0].new_content.replace("\r\n", "\n"),
            )
        finally:
            vcs.cleanup()

    def test_add_delete_rebuild_empty_and_binary_files_use_net_endpoints(self):
        self.write("Delete.java", "delete-old\n")
        self.write("Rebuild.java", "rebuild-old\n")
        self.write_bytes("Binary.bin", b"\x00old")
        self.commit("baseline")

        self.write("Added.java", "added\n")
        self.write_bytes("Empty.txt", b"")
        self.write_bytes("Binary.bin", b"\x00new")
        run([self.git, "rm", "Delete.java", "Rebuild.java"], self.repo)
        first = self.commit("selected add delete binary")
        self.write("Rebuild.java", "rebuild-new\n")
        last = self.commit("selected rebuild")

        vcs = GitMultiVersionVCS(self.repo, [first, last])
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual(ChangeType.ADDED, files["Added.java"])
            self.assertEqual(ChangeType.ADDED, files["Empty.txt"])
            self.assertEqual(ChangeType.MODIFIED, files["Binary.bin"])
            self.assertEqual(ChangeType.DELETED, files["Delete.java"])
            self.assertEqual(ChangeType.MODIFIED, files["Rebuild.java"])
            self.assertEqual(b"", vcs.get_file_content_bytes(vcs.new_version_label, "Empty.txt"))
            self.assertEqual(b"\x00old", vcs.get_file_content_bytes(vcs.old_version_label, "Binary.bin"))
            self.assertEqual(b"\x00new", vcs.get_file_content_bytes(vcs.new_version_label, "Binary.bin"))

            diff = DiffEngine(vcs).generate_diff(vcs.old_version_label, vcs.new_version_label)
            with project_temp_dir() as export_root:
                old_dir = os.path.join(export_root, "old")
                new_dir = os.path.join(export_root, "new")
                FileExporter(diff, vcs).export(old_dir, new_dir)
                with open(os.path.join(new_dir, "Binary.bin"), "rb") as handle:
                    self.assertEqual(b"\x00new", handle.read())
                with open(os.path.join(new_dir, "Empty.txt"), "rb") as handle:
                    self.assertEqual(b"", handle.read())
        finally:
            vcs.cleanup()

    def test_selected_merge_commit_uses_first_parent_net_change(self):
        self.write("base.txt", "base\n")
        self.commit("baseline")
        run([self.git, "checkout", "-b", "feature"], self.repo)
        self.write("feature.txt", "feature\n")
        self.commit("feature change")
        run([self.git, "checkout", "main"], self.repo)
        self.write("main.txt", "main\n")
        self.commit("main change")
        run([self.git, "merge", "--no-ff", "feature", "-m", "merge feature"], self.repo)
        merge_commit = run([self.git, "rev-parse", "HEAD"], self.repo).stdout.strip()

        vcs = GitMultiVersionVCS(self.repo, [merge_commit])
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual("feature.txt", files[0].path)
            self.assertEqual(ChangeType.ADDED, files[0].change_type)
        finally:
            vcs.cleanup()

    def test_commit_outside_current_first_parent_history_is_rejected(self):
        self.write("A.java", "base\n")
        self.commit("baseline")
        run([self.git, "checkout", "-b", "side"], self.repo)
        self.write("side.java", "side\n")
        side_commit = self.commit("side only")
        run([self.git, "checkout", "main"], self.repo)

        with self.assertRaisesRegex(RuntimeError, "第一父历史"):
            GitMultiVersionVCS(self.repo, [side_commit])

    def test_low_similarity_possible_rename_fails_closed(self):
        original = "".join(f"original-{index:03d}\n" for index in range(200))
        self.write("A.java", original)
        self.commit("baseline")
        self.write("A.java", original + "selected-before\n")
        first = self.commit("selected A")

        run([self.git, "mv", "A.java", "B.java"], self.repo)
        retained = "".join(f"original-{index:03d}\n" for index in range(20))
        replacement = "".join(f"replacement-{index:03d}\n" for index in range(180))
        self.write("B.java", retained + replacement)
        self.commit("low similarity rename")
        self.write("B.java", retained + replacement + "selected-after\n")
        last = self.commit("selected B")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_unrelated_low_similarity_rename_between_selected_commits_is_ignored(self):
        original = "".join(f"original-{index:03d}\n" for index in range(200))
        self.write("A.java", "A-base\n")
        self.write("B.java", "B-base\n")
        self.write("unrelated/Old.java", original)
        self.commit("baseline unrelated low similarity rename")

        self.write("A.java", "A-selected\n")
        first = self.commit("selected A")
        run([self.git, "mv", "unrelated/Old.java", "unrelated/New.java"], self.repo)
        retained = "".join(f"original-{index:03d}\n" for index in range(20))
        replacement = "".join(f"replacement-{index:03d}\n" for index in range(180))
        self.write("unrelated/New.java", retained + replacement)
        self.commit("unselected unrelated low similarity rename")
        self.write("B.java", "B-selected\n")
        last = self.commit("selected B")

        vcs = GitMultiVersionVCS(self.repo, [first, last])
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual(
                {"A.java": ChangeType.MODIFIED, "B.java": ChangeType.MODIFIED},
                files,
            )
        finally:
            vcs.cleanup()

    def test_unrelated_intermediate_type_change_does_not_block_selected_files(self):
        self.write("A.java", "A-base\n")
        self.write("B.java", "B-base\n")
        self.write("Unrelated.java", "regular\n")
        self.commit("baseline unrelated type change")

        self.write("A.java", "A-selected\n")
        first = self.commit("selected A before unrelated type change")
        self.stage_symlink("Unrelated.java", "target.txt")
        run([self.git, "commit", "-m", "unselected unrelated type change"], self.repo)
        self.write("B.java", "B-selected\n")
        run([self.git, "add", "B.java"], self.repo)
        run([self.git, "commit", "-m", "selected B after unrelated type change"], self.repo)
        last = run([self.git, "rev-parse", "HEAD"], self.repo).stdout.strip()

        vcs = GitMultiVersionVCS(self.repo, [first, last])
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual(
                {"A.java": ChangeType.MODIFIED, "B.java": ChangeType.MODIFIED},
                files,
            )
        finally:
            vcs.cleanup()

    def test_selected_non_regular_type_change_still_fails_closed(self):
        self.write("Link.java", "regular\n")
        self.commit("baseline selected type change")
        self.stage_symlink("Link.java", "target.txt")
        run([self.git, "commit", "-m", "selected symlink endpoint"], self.repo)
        selected = run([self.git, "rev-parse", "HEAD"], self.repo).stdout.strip()

        with self.assertRaisesRegex(RuntimeError, "不是普通文件"):
            GitMultiVersionVCS(self.repo, [selected])

    def test_zero_similarity_possible_rename_fails_closed(self):
        original = "".join(f"old-{index:03d}\n" for index in range(80))
        self.write("A.java", original)
        self.commit("baseline")
        self.write("A.java", original + "selected-before\n")
        first = self.commit("selected A")

        run([self.git, "mv", "A.java", "B.java"], self.repo)
        self.write("B.java", "".join(f"new-{index:03d}\n" for index in range(80)))
        self.commit("zero similarity move")
        self.write("B.java", "rewritten\nselected-after\n")
        last = self.commit("selected B")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_duplicate_content_rename_source_ambiguity_fails_closed(self):
        common = "same-content\n"
        self.write("src/Chosen.java", common)
        self.write("B.java", common)
        self.commit("baseline duplicates")
        self.write("src/Chosen.java", "selected-before\n")
        first = self.commit("selected chosen")
        self.write("src/Chosen.java", common)
        self.commit("restore duplicate content")

        run([self.git, "mv", "src/Chosen.java", "src/New.java"], self.repo)
        run([self.git, "rm", "B.java"], self.repo)
        self.commit("ambiguous rename pairing")
        self.write("src/New.java", "selected-after\n")
        last = self.commit("selected new")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_multiple_duplicate_content_renames_fail_closed(self):
        common = "same-content\n"
        self.write("one/A.java", common)
        self.write("two/B.java", common)
        self.commit("baseline duplicate rename sources")
        self.write("one/A.java", "selected-before\n")
        first = self.commit("selected one A")
        self.write("one/A.java", common)
        self.commit("restore duplicate")

        run([self.git, "mv", "one/A.java", "one/B.java"], self.repo)
        run([self.git, "mv", "two/B.java", "two/A.java"], self.repo)
        self.commit("two ambiguous renames")
        self.write("one/B.java", "selected-after\n")
        last = self.commit("selected one B")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_multiple_high_similarity_renames_fail_closed(self):
        common = "".join(f"shared-{index:03d}\n" for index in range(200))
        original_a = common + "old-A\n"
        original_b = common + "old-B\n"
        self.write("one/A.java", original_a)
        self.write("two/B.java", original_b)
        self.commit("baseline similar rename sources")
        self.write("one/A.java", "selected-before\n")
        first = self.commit("selected similar one A")
        self.write("one/A.java", original_a)
        self.commit("restore similar source")

        run([self.git, "mv", "one/A.java", "one/B.java"], self.repo)
        run([self.git, "mv", "two/B.java", "two/A.java"], self.repo)
        self.write("one/B.java", common + "dest-one\n")
        self.write("two/A.java", common + "dest-two\n")
        self.commit("two high similarity renames")
        self.write("one/B.java", common + "selected-after\n")
        last = self.commit("selected similar one B")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_reliable_rename_and_unrelated_delete_are_not_false_ambiguity(self):
        self.write("A.java", "AAA\n")
        self.write("C.java", "CCC\n")
        self.commit("baseline unrelated files")
        self.write("C.java", "selected C\n")
        first = self.commit("selected C")

        run([self.git, "mv", "A.java", "B.java"], self.repo)
        run([self.git, "rm", "C.java"], self.repo)
        self.commit("rename A and delete unrelated C")
        self.write("B.java", "selected B\n")
        last = self.commit("selected B")

        vcs = GitMultiVersionVCS(self.repo, [first, last])
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual(
                {"B.java": ChangeType.MODIFIED, "C.java": ChangeType.MODIFIED},
                files,
            )
        finally:
            vcs.cleanup()

    def test_high_similarity_competing_rename_source_fails_closed(self):
        common = "".join(f"shared-{index:03d}\n" for index in range(200))
        chosen = common + "".join(f"chosen-{index:02d}\n" for index in range(20))
        other = common + "other-old\n"
        target = common + "other-new\n"
        self.write("src/Chosen.java", chosen)
        self.write("B.java", other)
        self.commit("baseline competing rename sources")
        self.write("src/Chosen.java", "selected-before\n")
        first = self.commit("selected chosen before rename")
        self.write("src/Chosen.java", chosen)
        self.commit("restore chosen source")

        run([self.git, "mv", "src/Chosen.java", "src/New.java"], self.repo)
        self.write("src/New.java", target)
        run([self.git, "rm", "B.java"], self.repo)
        rename_commit = self.commit("high similarity competing rename")
        status = self._git_status(rename_commit)
        self.assertIn("src/New.java", status)
        self.assertIn("src/Chosen.java", status)

        self.write("src/New.java", target + "selected-after\n")
        last = self.commit("selected new after rename")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_move_split_across_unselected_commits_fails_closed(self):
        self.write("A.java", "base\n")
        self.commit("baseline split move")
        self.write("A.java", "selected-before-split-move\n")
        first = self.commit("selected A before split move")

        run([self.git, "rm", "A.java"], self.repo)
        self.commit("delete A in separate commit")
        self.write("B.java", "selected-before-split-move\n")
        self.commit("add B in later commit")
        self.write("B.java", "selected-after-split-move\n")
        last = self.commit("selected B after split move")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_git_native_rename_score_drives_competing_candidate_check(self):
        self.write("src/Chosen.java", "A" * 10000)
        self.write("B.java", "X" * 10000)
        self.commit("baseline long single line sources")
        self.write("src/Chosen.java", "selected-before\n")
        first = self.commit("selected long chosen")
        self.write("src/Chosen.java", "A" * 10000)
        self.commit("restore long chosen")

        run([self.git, "mv", "src/Chosen.java", "src/New.java"], self.repo)
        self.write("src/New.java", "Z" * 10000)
        run([self.git, "rm", "B.java"], self.repo)
        rename_commit = self.commit("git native single line rename score")
        status = self._git_status(rename_commit)
        self.assertRegex(status, r"R\d+\s+B\.java\s+src/New\.java")
        self.assertRegex(status, r"D\s+src/Chosen\.java")

        self.write("src/New.java", "selected-after\n")
        last = self.commit("selected long new")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_cross_commit_candidate_ignores_same_path_rebuild_noise(self):
        moved_content = "selected-source-content\n"
        self.write("A.java", "base\n")
        self.commit("baseline same path rebuild")
        self.write("A.java", moved_content)
        first = self.commit("selected A before rebuild")

        run([self.git, "rm", "A.java"], self.repo)
        self.commit("delete A before split identity")
        self.write("A.java", "unrelated rebuilt A\n")
        self.write("B.java", moved_content)
        self.commit("rebuild A and add moved B")
        self.write("B.java", "selected-after-rebuild\n")
        last = self.commit("selected B after rebuild")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_pending_delete_competes_with_later_rename_target(self):
        common = "same-content\n"
        self.write("A.java", common)
        self.write("X.java", common)
        self.commit("baseline pending source and rename source")
        self.write("A.java", "selected-before\n")
        first = self.commit("selected pending A")
        self.write("A.java", common)
        self.commit("restore pending A")

        run([self.git, "rm", "A.java"], self.repo)
        self.commit("delete A before later rename")
        run([self.git, "mv", "X.java", "B.java"], self.repo)
        self.commit("rename competing X to B")
        self.write("B.java", "selected-after\n")
        last = self.commit("selected competing B")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_pending_delete_competes_with_rename_reusing_same_path(self):
        common = "same-content\n"
        self.write("A.java", common)
        self.write("X.java", common)
        self.commit("baseline reused target path")
        self.write("A.java", "selected-before\n")
        first = self.commit("selected A before path reuse")
        self.write("A.java", common)
        self.commit("restore A before path reuse")

        run([self.git, "rm", "A.java"], self.repo)
        self.commit("delete A before target path reuse")
        run([self.git, "mv", "X.java", "A.java"], self.repo)
        self.commit("rename X onto reused A path")
        self.write("A.java", "selected-after\n")
        last = self.commit("selected reused A path")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_move_over_existing_target_as_delete_modify_fails_closed(self):
        source = "source-content\n"
        self.write("A.java", source)
        self.write("B.java", "old-target\n")
        self.commit("baseline overwrite move")
        self.write("A.java", "selected-before\n")
        first = self.commit("selected A before overwrite")
        self.write("A.java", source)
        self.commit("restore A before overwrite")

        os.replace(
            os.path.join(self.repo, "A.java"),
            os.path.join(self.repo, "B.java"),
        )
        overwrite_commit = self.commit("move A over existing B")
        status = self._git_status(overwrite_commit)
        self.assertRegex(status, r"D\s+A\.java")
        self.assertRegex(status, r"M\s+B\.java")
        self.write("B.java", "selected-after\n")
        last = self.commit("selected B after overwrite")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def test_rename_source_competes_with_modified_existing_target(self):
        common = "same-content\n"
        self.write("A.java", common)
        self.write("X.java", common)
        self.write("C.java", "old-target\n")
        self.commit("baseline rename modify competition")
        self.write("A.java", "selected-before\n")
        first = self.commit("selected A before competing moves")
        self.write("A.java", common)
        self.commit("restore A before competing moves")

        run([self.git, "mv", "X.java", "B.java"], self.repo)
        os.replace(
            os.path.join(self.repo, "A.java"),
            os.path.join(self.repo, "C.java"),
        )
        competing_commit = self.commit("rename X and move A over C")
        status = self._git_status(competing_commit)
        self.assertRegex(status, r"R\d+\s+A\.java\s+B\.java")
        self.assertRegex(status, r"M\s+C\.java")
        self.assertRegex(status, r"D\s+X\.java")
        self.write("C.java", "selected-after\n")
        last = self.commit("selected C after competing moves")

        with self.assertRaisesRegex(RuntimeError, "无法唯一确认"):
            GitMultiVersionVCS(self.repo, [first, last])

    def _git_status(self, commit):
        parent = run([self.git, "rev-parse", f"{commit}^"], self.repo).stdout.strip()
        return run(
            [
                self.git,
                "diff",
                "--name-status",
                "--find-renames=50%",
                parent,
                commit,
            ],
            self.repo,
        ).stdout

    def test_shallow_boundary_is_not_treated_as_root_commit(self):
        self.write("A.java", "base\n")
        self.commit("baseline")
        self.write("A.java", "selected\n")
        selected = self.commit("selected")

        with project_temp_dir() as clone_root:
            shallow = os.path.join(clone_root, "repo")
            run(
                [self.git, "clone", "--depth", "1", pathlib.Path(self.repo).resolve().as_uri(), shallow],
                clone_root,
            )
            with self.assertRaisesRegex(RuntimeError, "浅克隆边界"):
                GitMultiVersionVCS(shallow, [selected])

    def test_raw_line_ending_change_survives_checkout_byte_normalization(self):
        run([self.git, "config", "core.autocrlf", "true"], self.repo)
        self.write_bytes("Format.txt", b"first\nsecond\n")
        self.commit("baseline LF")

        self.write_bytes("Format.txt", b"first\r\nsecond\r\n")
        run([self.git, "-c", "core.autocrlf=false", "add", "Format.txt"], self.repo)
        run([self.git, "commit", "-m", "selected CRLF blob"], self.repo)
        selected = run([self.git, "rev-parse", "HEAD"], self.repo).stdout.strip()

        vcs = GitMultiVersionVCS(self.repo, [selected])
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            diff = DiffEngine(vcs).generate_diff("ui-selected-version", vcs.new_version_label)
            self.assertEqual("F", diff.files[0].report_type)
            self.assertIn("换行符：LF → CRLF", diff.files[0].format_details)
            self.assertEqual(
                b"first\r\nsecond\r\n",
                vcs.get_file_content_bytes("ui-selected-version", "Format.txt"),
            )
            self.assertEqual(
                b"first\r\nsecond\r\n",
                vcs.get_file_content_bytes(vcs.new_version_label, "Format.txt"),
            )
        finally:
            vcs.cleanup()


@unittest.skipUnless(shutil.which("svn") and shutil.which("svnadmin"), "需要 svn 和 svnadmin")
class SVNFileEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = project_temp_dir()
        self.root = self.temp.name
        self.repo = os.path.join(self.root, "repo")
        self.wc = os.path.join(self.root, "wc")
        self.svn = shutil.which("svn")
        self.svnadmin = shutil.which("svnadmin")
        run([self.svnadmin, "create", self.repo], self.root)
        self.url = pathlib.Path(self.repo).resolve().as_uri() + "/project"
        run([self.svn, "mkdir", self.url, "-m", "create project"], self.root)
        run([self.svn, "checkout", self.url, self.wc], self.root)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, content):
        target = os.path.join(self.wc, path)
        os.makedirs(os.path.dirname(target) or self.wc, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)

    def commit(self, message):
        run([self.svn, "add", "--force", "."], self.wc)
        output = run([self.svn, "commit", "-m", message], self.wc).stdout
        match = re.search(r"Committed revision (\d+)", output)
        self.assertIsNotNone(match, output)
        return int(match.group(1))

    def test_normal_and_multi_svn_preserve_executable_property_only_change(self):
        self.write("Script.sh", "echo ok\n")
        baseline = self.commit("baseline")
        run([self.svn, "propset", "svn:executable", "*", "Script.sh"], self.wc)
        selected = self.commit("make executable")

        normal = SVNVCS(self.wc, svn_path=self.svn)
        normal_files = normal.get_changed_files(str(baseline), str(selected))
        self.assertEqual(1, len(normal_files))
        self.assertIn("SVN 可执行属性", normal_files[0].metadata_changes[0])
        self.assertTrue(normal_files[0].new_executable)

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{selected}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.MODIFIED, files[0].change_type)
            self.assertIn("SVN 可执行属性", files[0].metadata_changes[0])
            self.assertTrue(files[0].new_executable)
        finally:
            vcs.cleanup()

    def test_normal_svn_project_root_move_uses_historical_endpoint_urls(self):
        self.write("A.java", "base-before-root-move\n")
        baseline = self.commit("baseline before ordinary svn root move")

        renamed_url = pathlib.Path(self.repo).resolve().as_uri() + "/ordinary-renamed"
        run([self.svn, "move", self.url, renamed_url, "-m", "move ordinary root"], self.root)
        run([self.svn, "switch", renamed_url, self.wc], self.root)
        self.url = renamed_url
        self.write("A.java", "selected-after-root-move\n")
        selected = self.commit("change after ordinary root move")

        vcs = SVNVCS(self.wc, svn_path=self.svn)
        files = vcs.get_changed_files(str(baseline), str(selected))
        self.assertEqual(["A.java"], [item.path for item in files])
        self.assertEqual(
            "base-before-root-move\n",
            normalized(vcs.get_file_content_bytes(str(baseline), "A.java")),
        )
        self.assertEqual(
            "selected-after-root-move\n",
            normalized(vcs.get_file_content_bytes(str(selected), "A.java")),
        )

    def test_normal_svn_content_reads_ignore_later_working_copy_switch(self):
        self.write("Pinned.txt", "project-old\n")
        baseline = self.commit("ordinary svn pinned baseline")
        self.write("Pinned.txt", "project-new\n")
        selected = self.commit("ordinary svn pinned selected")

        other_url = pathlib.Path(self.repo).resolve().as_uri() + "/other-project"
        run([self.svn, "copy", self.url, other_url, "-m", "copy other project"], self.root)
        run([self.svn, "switch", other_url, self.wc], self.root)
        self.write("Pinned.txt", "other-project-content\n")
        self.commit("change other project")
        run([self.svn, "switch", self.url, self.wc], self.root)

        vcs = SVNVCS(self.wc, svn_path=self.svn)
        files = vcs.get_changed_files(str(baseline), str(selected))
        self.assertEqual(["Pinned.txt"], [item.path for item in files])

        run([self.svn, "switch", other_url, self.wc], self.root)
        self.assertEqual(
            "project-old\n",
            normalized(vcs.get_file_content_bytes(str(baseline), "Pinned.txt")),
        )
        self.assertEqual(
            "project-new\n",
            normalized(vcs.get_file_content_bytes(str(selected), "Pinned.txt")),
        )

    def test_svn_selected_project_root_move_with_externals_fails_closed(self):
        self.write("A.java", "base\n")
        self.commit("baseline before root externals move")

        repo_root_url = pathlib.Path(self.repo).resolve().as_uri()
        root_wc = os.path.join(self.root, "root-externals-wc")
        run([self.svn, "checkout", repo_root_url, root_wc], self.root)
        run([self.svn, "move", "project", "root-with-externals"], root_wc)
        run(
            [
                self.svn, "propset", "svn:externals", "^/dependency vendor",
                "root-with-externals",
            ],
            root_wc,
        )
        output = run(
            [self.svn, "commit", "-m", "move root and add externals"], root_wc
        ).stdout
        selected_match = re.search(r"Committed revision (\d+)", output)
        self.assertIsNotNone(selected_match, output)
        selected = int(selected_match.group(1))

        renamed_url = repo_root_url + "/root-with-externals"
        run([self.svn, "switch", "--ignore-externals", renamed_url, self.wc], self.root)
        self.url = renamed_url
        with self.assertRaisesRegex(RuntimeError, "svn:externals"):
            SVNMultiVersionVCS(
                self.wc, [f"r{selected}"], svn_path=self.svn
            )

    def test_each_file_uses_its_own_svn_revision_endpoints(self):
        self.write("A.java", "A-base\n")
        self.write("B.java", "B-base\n")
        self.commit("baseline")
        self.write("B.java", "B-selected\n")
        selected_b = self.commit("selected B")
        self.write("A.java", "A-unselected\n")
        self.commit("unselected A")
        self.write("A.java", "A-selected\n")
        selected_a = self.commit("selected A")
        self.write("B.java", "B-later\n")
        self.commit("later B")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{selected_b}", f"r{selected_a}"], svn_path=self.svn
        )
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual({"A.java": ChangeType.MODIFIED, "B.java": ChangeType.MODIFIED}, files)
            self.assertEqual(
                "A-unselected\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "A.java")),
            )
            self.assertEqual(
                "A-selected\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "A.java")),
            )
            self.assertEqual(
                "B-base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "B.java")),
            )
            self.assertEqual(
                "B-selected\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "B.java")),
            )
        finally:
            vcs.cleanup()

    def test_unselected_svn_directory_move_is_traced_between_selected_changes(self):
        self.write("old/One.java", "base\n")
        self.commit("baseline")
        self.write("old/One.java", "selected-before\n")
        first = self.commit("selected old path")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        self.commit("unselected directory move")
        self.write("new/One.java", "selected-after\n")
        last = self.commit("selected new path")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/One.java", files[0].old_path)
            self.assertEqual("new/One.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_add_delete_and_rebuild_collapse_to_net_results(self):
        self.write("Delete.java", "delete-old\n")
        self.write("Rebuild.java", "rebuild-old\n")
        self.commit("baseline")
        self.write("Added.java", "added\n")
        self.write("Empty.txt", "")
        run([self.svn, "delete", "Delete.java", "Rebuild.java"], self.wc)
        first = self.commit("selected add delete")
        self.write("Rebuild.java", "rebuild-new\n")
        last = self.commit("selected rebuild")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual(ChangeType.ADDED, files["Added.java"])
            self.assertEqual(ChangeType.ADDED, files["Empty.txt"])
            self.assertEqual(ChangeType.DELETED, files["Delete.java"])
            self.assertEqual(ChangeType.MODIFIED, files["Rebuild.java"])
            self.assertEqual(b"", vcs.get_file_content_bytes(vcs.new_version_label, "Empty.txt"))
        finally:
            vcs.cleanup()

    def test_svn_project_root_move_keeps_revision_specific_prefixes(self):
        self.write("A.java", "base\n")
        self.commit("baseline")
        self.write("A.java", "selected-before-root-move\n")
        first = self.commit("selected before root move")

        renamed_url = pathlib.Path(self.repo).resolve().as_uri() + "/renamed-project"
        run([self.svn, "move", self.url, renamed_url, "-m", "move project root"], self.root)
        run([self.svn, "switch", renamed_url, self.wc], self.root)
        self.url = renamed_url
        self.write("A.java", "unselected-after-root-move\n")
        self.commit("unselected after root move")
        self.write("A.java", "selected-after-root-move\n")
        last = self.commit("selected after root move")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual("A.java", files[0].path)
            self.assertEqual(
                "base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "A.java")),
            )
            self.assertEqual(
                "selected-after-root-move\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "A.java")),
            )
        finally:
            vcs.cleanup()

    def test_svn_project_root_move_after_last_selection_only_changes_path_mapping(self):
        self.write("A.java", "base\n")
        self.commit("baseline")
        self.write("A.java", "selected-before-later-root-move\n")
        selected = self.commit("selected before later root move")

        renamed_url = pathlib.Path(self.repo).resolve().as_uri() + "/later-renamed-project"
        run([self.svn, "move", self.url, renamed_url, "-m", "later root move"], self.root)
        run([self.svn, "switch", renamed_url, self.wc], self.root)
        self.url = renamed_url
        self.write("A.java", "later-unselected-content\n")
        self.commit("later unselected content")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{selected}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(
                "base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "A.java")),
            )
            self.assertEqual(
                "selected-before-later-root-move\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "A.java")),
            )
        finally:
            vcs.cleanup()

    def test_svn_delayed_copyfrom_reconnects_deleted_file_identity(self):
        self.write("A.java", "base\n")
        self.commit("baseline")
        self.write("A.java", "selected-before-delete\n")
        first = self.commit("selected A")
        run([self.svn, "delete", "A.java"], self.wc)
        self.commit("delete A")

        run(
            [
                self.svn,
                "copy",
                f"{self.url}/A.java@{first}",
                f"{self.url}/B.java",
                "-m",
                "restore as B",
            ],
            self.root,
        )
        run([self.svn, "update"], self.wc)
        self.write("B.java", "selected-after-restore\n")
        last = self.commit("selected B")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("A.java", files[0].old_path)
            self.assertEqual("B.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_directory_move_with_child_rename_uses_explicit_copyfrom(self):
        self.write("old/A.java", "base\n")
        self.commit("baseline")
        self.write("old/A.java", "selected-before-move\n")
        first = self.commit("selected old A")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "move", "new/A.java", "new/B.java"], self.wc)
        self.commit("move directory and rename child")
        self.write("new/B.java", "selected-after-move\n")
        last = self.commit("selected new B")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/A.java", files[0].old_path)
            self.assertEqual("new/B.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_directory_move_with_ordinary_child_copy_keeps_inherited_identity(self):
        self.write("old/A.java", "base\n")
        self.commit("baseline directory move with child copy")
        self.write("old/A.java", "selected-before-move\n")
        first = self.commit("selected old A before child copy")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "copy", "new/A.java", "new/B.java"], self.wc)
        self.commit("move directory and ordinarily copy inherited child")
        self.write("new/A.java", "selected-after-move\n")
        last = self.commit("selected inherited new A")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/A.java", files[0].old_path)
            self.assertEqual("new/A.java", files[0].path)
            self.assertEqual(
                "base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "old/A.java")),
            )
            self.assertEqual(
                "selected-after-move\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "new/A.java")),
            )
        finally:
            vcs.cleanup()

    def test_svn_nested_directory_move_uses_most_specific_copy_context(self):
        self.write("old/sub/A.java", "base\n")
        self.commit("baseline nested directory move with child copy")
        self.write("old/sub/A.java", "selected-before-nested-move\n")
        first = self.commit("selected nested old A")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "move", "new/sub", "new/other"], self.wc)
        run([self.svn, "copy", "new/other/A.java", "new/other/B.java"], self.wc)
        self.commit("nested directory moves and ordinary child copy")
        self.write("new/other/A.java", "selected-after-nested-move\n")
        last = self.commit("selected nested new A")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/sub/A.java", files[0].old_path)
            self.assertEqual("new/other/A.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_selected_nested_directory_move_has_one_identity_chain(self):
        self.write("old/sub/A.java", "base\n")
        self.commit("baseline selected nested directory move")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "move", "new/sub", "new/renamed"], self.wc)
        selected = self.commit("selected nested directory move")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{selected}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/sub/A.java", files[0].old_path)
            self.assertEqual("new/renamed/A.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_selected_directory_move_child_moved_out_is_not_deleted_twice(self):
        self.write("old/A.java", "base\n")
        self.commit("baseline selected child moved out")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "move", "new/A.java", "Top.java"], self.wc)
        selected = self.commit("selected directory move child moved out")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{selected}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/A.java", files[0].old_path)
            self.assertEqual("Top.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_unselected_directory_move_child_replaces_external_target(self):
        self.write("old/A.java", "A-base\n")
        self.write("Top.java", "Top-base\n")
        self.commit("baseline external target replacement")
        self.write("old/A.java", "A-selected-before\n")
        first = self.commit("selected A before external replacement")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "delete", "Top.java"], self.wc)
        run([self.svn, "move", "new/A.java", "Top.java"], self.wc)
        self.commit("unselected child replaces external target")
        self.write("Top.java", "Top-selected-after\n")
        last = self.commit("selected Top after external replacement")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/A.java", files[0].old_path)
            self.assertEqual("Top.java", files[0].path)
            self.assertEqual(
                "A-base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "old/A.java")),
            )
            self.assertEqual(
                "Top-selected-after\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "Top.java")),
            )
        finally:
            vcs.cleanup()

    def test_svn_directory_move_child_replace_keeps_rename_and_replaced_delete(self):
        self.write("old/A.java", "old-A\n")
        self.write("old/B.java", "old-B\n")
        self.commit("baseline directory move child replace")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "delete", "new/B.java"], self.wc)
        run([self.svn, "move", "new/A.java", "new/B.java"], self.wc)
        selected = self.commit("selected directory move child replace")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{selected}"], svn_path=self.svn
        )
        try:
            files = {
                (item.old_path, item.path): item.change_type
                for item in vcs.get_changed_files()
            }
            self.assertEqual(
                {
                    ("old/A.java", "new/B.java"): ChangeType.RENAMED,
                    ("", "old/B.java"): ChangeType.DELETED,
                },
                files,
            )
        finally:
            vcs.cleanup()

    def test_svn_directory_move_one_source_multiple_targets_degrades_to_delete_adds(self):
        self.write("old/A.java", "source\n")
        self.commit("baseline directory move split copy")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        run([self.svn, "copy", "new/A.java", "new/B.java"], self.wc)
        run([self.svn, "copy", "new/A.java", "new/C.java"], self.wc)
        run([self.svn, "delete", "new/A.java"], self.wc)
        selected = self.commit("selected directory move split copy")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{selected}"], svn_path=self.svn
        )
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual(
                {
                    "old/A.java": ChangeType.DELETED,
                    "new/B.java": ChangeType.ADDED,
                    "new/C.java": ChangeType.ADDED,
                },
                files,
            )
        finally:
            vcs.cleanup()

    def test_svn_ordinary_directory_copy_stays_independent_after_child_delete(self):
        self.write("old/A.java", "base-A\n")
        self.write("old/Gone.java", "gone\n")
        self.commit("baseline copy source")
        self.write("old/A.java", "selected-old-A\n")
        first = self.commit("selected old A")
        run([self.svn, "delete", "old/Gone.java"], self.wc)
        self.commit("delete unrelated child")
        run([self.svn, "update"], self.wc)
        run([self.svn, "copy", "old", "new"], self.wc)
        self.commit("ordinary directory copy")
        self.write("new/A.java", "selected-new-A\n")
        last = self.commit("selected new A")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = {item.path: item.change_type for item in vcs.get_changed_files()}
            self.assertEqual(
                {
                    "old/A.java": ChangeType.MODIFIED,
                    "new/A.java": ChangeType.MODIFIED,
                },
                files,
            )
        finally:
            vcs.cleanup()

    def test_svn_ancestor_directory_move_updates_project_prefix(self):
        repo_root_url = pathlib.Path(self.repo).resolve().as_uri()
        container_url = repo_root_url + "/container"
        nested_url = container_url + "/project"
        run([self.svn, "mkdir", container_url, "-m", "create container"], self.root)
        run([self.svn, "move", self.url, nested_url, "-m", "nest project"], self.root)
        run([self.svn, "switch", nested_url, self.wc], self.root)
        self.url = nested_url

        self.write("A.java", "base\n")
        self.commit("baseline nested project")
        self.write("A.java", "selected-before-ancestor-move\n")
        first = self.commit("selected before ancestor move")

        renamed_container = repo_root_url + "/renamed"
        renamed_project = renamed_container + "/project"
        run(
            [self.svn, "move", container_url, renamed_container, "-m", "move ancestor"],
            self.root,
        )
        run([self.svn, "switch", renamed_project, self.wc], self.root)
        self.url = renamed_project
        self.write("A.java", "selected-after-ancestor-move\n")
        last = self.commit("selected after ancestor move")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(
                "base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "A.java")),
            )
            self.assertEqual(
                "selected-after-ancestor-move\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "A.java")),
            )
        finally:
            vcs.cleanup()

    def test_svn_directory_list_uses_old_root_after_later_project_move(self):
        self.write("old/A.java", "base\n")
        self.commit("baseline old directory")
        self.write("old/A.java", "selected-before-directory-move\n")
        first = self.commit("selected old directory")
        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "old", "new"], self.wc)
        self.commit("move inner directory")

        renamed_url = pathlib.Path(self.repo).resolve().as_uri() + "/renamed-after-inner"
        run([self.svn, "move", self.url, renamed_url, "-m", "move project root later"], self.root)
        run([self.svn, "switch", renamed_url, self.wc], self.root)
        self.url = renamed_url
        self.write("new/A.java", "selected-after-both-moves\n")
        last = self.commit("selected new directory")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("old/A.java", files[0].old_path)
            self.assertEqual("new/A.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_same_revision_ancestor_move_and_child_rename_keeps_identity(self):
        repo_root_url = pathlib.Path(self.repo).resolve().as_uri()
        container_url = repo_root_url + "/container"
        nested_url = container_url + "/project"
        run([self.svn, "mkdir", container_url, "-m", "create container"], self.root)
        run([self.svn, "move", self.url, nested_url, "-m", "nest project"], self.root)
        run([self.svn, "switch", nested_url, self.wc], self.root)
        self.url = nested_url

        self.write("A.java", "base\n")
        self.commit("baseline nested A")
        self.write("A.java", "selected-before-combined-move\n")
        first = self.commit("selected A before combined move")

        root_wc = os.path.join(self.root, "root-wc")
        run([self.svn, "checkout", repo_root_url, root_wc], self.root)
        run([self.svn, "move", "container", "renamed"], root_wc)
        run(
            [self.svn, "move", "renamed/project/A.java", "renamed/project/B.java"],
            root_wc,
        )
        run([self.svn, "commit", "-m", "move ancestor and rename child"], root_wc)

        renamed_url = repo_root_url + "/renamed/project"
        run([self.svn, "switch", renamed_url, self.wc], self.root)
        self.url = renamed_url
        self.write("B.java", "selected-after-combined-move\n")
        last = self.commit("selected B after combined move")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("A.java", files[0].old_path)
            self.assertEqual("B.java", files[0].path)
            self.assertEqual(
                "base\n",
                normalized(vcs.get_file_content_bytes(vcs.old_version_label, "A.java")),
            )
            self.assertEqual(
                "selected-after-combined-move\n",
                normalized(vcs.get_file_content_bytes(vcs.new_version_label, "B.java")),
            )
        finally:
            vcs.cleanup()

    def test_svn_directory_move_survives_deleted_source_ancestor(self):
        self.write("parent/sub/A.java", "base\n")
        self.commit("baseline nested directory move")
        self.write("parent/sub/A.java", "selected-before-directory-move\n")
        first = self.commit("selected nested A")

        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "parent/sub", "new"], self.wc)
        run([self.svn, "delete", "parent"], self.wc)
        self.commit("move child directory and delete empty ancestor")
        self.write("new/A.java", "selected-after-directory-move\n")
        last = self.commit("selected moved directory A")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("parent/sub/A.java", files[0].old_path)
            self.assertEqual("new/A.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_file_move_survives_deleted_source_ancestor(self):
        self.write("parent/A.java", "base\n")
        self.commit("baseline nested file move")
        self.write("parent/A.java", "selected-before-file-move\n")
        first = self.commit("selected parent A")

        run([self.svn, "update"], self.wc)
        run([self.svn, "move", "parent/A.java", "B.java"], self.wc)
        run([self.svn, "delete", "parent"], self.wc)
        self.commit("move file and delete empty ancestor")
        self.write("B.java", "selected-after-file-move\n")
        last = self.commit("selected moved B")

        vcs = SVNMultiVersionVCS(
            self.wc, [f"r{first}", f"r{last}"], svn_path=self.svn
        )
        try:
            files = vcs.get_changed_files()
            self.assertEqual(1, len(files))
            self.assertEqual(ChangeType.RENAMED, files[0].change_type)
            self.assertEqual("parent/A.java", files[0].old_path)
            self.assertEqual("B.java", files[0].path)
        finally:
            vcs.cleanup()

    def test_svn_special_endpoint_fails_closed_before_net_zero_filter(self):
        self.write("Link.txt", "link target")
        self.commit("baseline regular file")
        run([self.svn, "propset", "svn:special", "*", "Link.txt"], self.wc)
        selected = self.commit("selected svn special")

        with self.assertRaisesRegex(RuntimeError, "svn:special"):
            SVNMultiVersionVCS(self.wc, [f"r{selected}"], svn_path=self.svn)


class DeliveryInstructionsTests(unittest.TestCase):
    @staticmethod
    def result(files):
        return DiffResult(
            project_path="demo",
            project_name="Demo",
            vcs_type="Fake",
            old_version="old",
            new_version="new",
            files=files,
        )

    def test_instructions_list_delete_and_rename_old_paths_with_project_name(self):
        result = self.result([
            FileDiff("src/Old.java", ChangeType.DELETED),
            FileDiff("src/C.java", ChangeType.RENAMED, old_path="src/B.java"),
            FileDiff("src/A.java", ChangeType.MODIFIED),
        ])
        with project_temp_dir() as root:
            output = os.path.join(root, DELIVERY_INSTRUCTIONS_FILENAME)
            write_delivery_instructions(
                [{"project_name": "Demo", "diff_result": result}], output
            )
            with open(output, "rb") as handle:
                raw = handle.read()

        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("[删除文件] Demo/src/Old.java", text)
        self.assertIn("[重命名旧路径] Demo/src/B.java", text)
        self.assertIn("Demo/src/B.java\r\n  -> Demo/src/C.java", text)
        self.assertNotIn("Demo/src/A.java", text)

    def test_instructions_are_generated_even_without_delete_or_rename(self):
        result = self.result([FileDiff("src/A.java", ChangeType.MODIFIED)])
        with project_temp_dir() as root:
            target = os.path.join(root, DELIVERY_INSTRUCTIONS_FILENAME)
            stage, returned_target = prepare_delivery_instructions(
                [{"project_name": "Demo", "diff_result": result}], target
            )
            try:
                self.assertEqual(os.path.abspath(target), returned_target)
                with open(stage, encoding="utf-8-sig") as handle:
                    text = handle.read()
                self.assertIn("本次无需要删除或重命名的旧文件", text)
            finally:
                if os.path.isfile(stage):
                    os.remove(stage)

    def test_instructions_include_executable_permission_actions(self):
        result = self.result([
            FileDiff(
                "bin/start.sh",
                ChangeType.MODIFIED,
                old_executable=False,
                new_executable=True,
            ),
            FileDiff(
                "bin/stop.sh",
                ChangeType.MODIFIED,
                old_executable=True,
                new_executable=False,
            ),
        ])
        with project_temp_dir() as root:
            output = os.path.join(root, DELIVERY_INSTRUCTIONS_FILENAME)
            write_delivery_instructions(
                [{"project_name": "Demo", "diff_result": result}], output
            )
            with open(output, encoding="utf-8-sig") as handle:
                text = handle.read()

        self.assertIn("[设置可执行权限（chmod +x）] Demo/bin/start.sh", text)
        self.assertIn("[移除可执行权限（chmod -x）] Demo/bin/stop.sh", text)

if __name__ == "__main__":
    unittest.main()
