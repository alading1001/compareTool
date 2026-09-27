import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from file_exporter import FileExporter
from main import CompareToolApp


def project_temp_dir():
    os.makedirs(".tmp", exist_ok=True)
    return tempfile.TemporaryDirectory(dir=".tmp")


class OutputCompatibilityAfterAug27Tests(unittest.TestCase):
    def test_git_descendant_output_rejects_tracked_source_target(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "repo")
            target = os.path.join(source, "oldVersion", "Demo")
            os.makedirs(source)
            occupied = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"100644 deadbeef 0\toldVersion/Demo/source.java\0",
                stderr=b"",
            )
            top = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=(os.path.abspath(source) + "\n").encode("utf-8"),
                stderr=b"",
            )

            with mock.patch(
                "main.GitVCS._find_git", return_value="git"
            ), mock.patch(
                "main.subprocess.run", side_effect=[top, occupied]
            ):
                with self.assertRaisesRegex(ValueError, "Git 已跟踪"):
                    CompareToolApp._validate_repository_output_targets(
                        source, "git", [target]
                    )

    def test_git_descendant_output_allows_untracked_previous_output(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "repo")
            target = os.path.join(source, "oldVersion", "Demo")
            os.makedirs(target)
            untracked = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            top = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=(os.path.abspath(source) + "\n").encode("utf-8"),
                stderr=b"",
            )

            with mock.patch(
                "main.GitVCS._find_git", return_value="git"
            ), mock.patch(
                "main.subprocess.run", side_effect=[top, untracked]
            ):
                CompareToolApp._validate_repository_output_targets(
                    source, "git_multi", [target]
                )

    def test_git_subdirectory_project_still_detects_tracked_target(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        with project_temp_dir() as root:
            repo = os.path.join(root, "repo")
            project = os.path.join(repo, "component")
            tracked = os.path.join(project, "source.txt")
            os.makedirs(project)
            subprocess.run([git, "init", "-q", repo], check=True)
            with open(tracked, "w", encoding="utf-8") as stream:
                stream.write("tracked")
            subprocess.run(
                [git, "add", "component/source.txt"],
                cwd=repo,
                check=True,
            )

            with self.assertRaisesRegex(ValueError, "Git 已跟踪"):
                CompareToolApp._validate_repository_output_targets(
                    project, "git", [tracked]
                )

    def test_git_subdirectory_project_detects_tracked_sibling_target(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        with project_temp_dir() as root:
            repo = os.path.join(root, "repo")
            project = os.path.join(repo, "component")
            sibling = os.path.join(repo, "delivery", "source.txt")
            os.makedirs(project)
            os.makedirs(os.path.dirname(sibling))
            subprocess.run([git, "init", "-q", repo], check=True)
            with open(sibling, "w", encoding="utf-8") as stream:
                stream.write("tracked sibling")
            subprocess.run(
                [git, "add", "delivery/source.txt"], cwd=repo, check=True
            )

            with self.assertRaisesRegex(ValueError, "Git 已跟踪"):
                CompareToolApp._validate_repository_output_targets(
                    project, "git", [sibling]
                )

    def test_git_subdirectory_project_allows_untracked_sibling_output(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        with project_temp_dir() as root:
            repo = os.path.join(root, "repo")
            project = os.path.join(repo, "component")
            output = os.path.join(repo, "delivery", "oldVersion")
            os.makedirs(project)
            os.makedirs(output)
            subprocess.run([git, "init", "-q", repo], check=True)

            CompareToolApp._validate_repository_output_targets(
                project, "git", [output]
            )

    def test_bare_git_repository_can_still_write_to_external_output(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        with project_temp_dir() as root:
            repository = os.path.join(root, "source.git")
            output = os.path.join(root, "delivery")
            subprocess.run([git, "init", "--bare", "-q", repository], check=True)

            CompareToolApp._validate_repository_output_targets(
                repository, "git", [output]
            )

            with self.assertRaisesRegex(ValueError, "裸仓库元数据"):
                CompareToolApp._validate_repository_output_targets(
                    repository, "git", [os.path.join(repository, "report.html")]
                )

    def test_svn_descendant_output_rejects_versioned_target(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "wc")
            target = os.path.join(source, "newVersion")
            os.makedirs(target)
            versioned = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=(
                    b'<?xml version="1.0"?><status><target path="x">'
                    b'<entry path="x"><wc-status item="normal" />'
                    b'</entry></target></status>'
                ),
                stderr=b"",
            )

            with mock.patch(
                "main.SVNVCS._find_svn", return_value="svn"
            ), mock.patch(
                "main.subprocess.run", return_value=versioned
            ):
                with self.assertRaisesRegex(ValueError, "SVN 已版本化"):
                    CompareToolApp._validate_repository_output_targets(
                        source, "svn", [target]
                    )

    def test_svn_subdirectory_project_detects_versioned_target(self):
        svn = shutil.which("svn")
        svnadmin = shutil.which("svnadmin")
        if not svn or not svnadmin:
            self.skipTest("svn command line tools are not installed")
        with project_temp_dir() as root:
            repository = os.path.join(root, "repository")
            working_copy = os.path.join(root, "working-copy")
            project = os.path.join(working_copy, "component")
            tracked = os.path.join(project, "source.txt")
            subprocess.run([svnadmin, "create", repository], check=True)
            subprocess.run(
                [svn, "checkout", "-q", Path(repository).as_uri(), working_copy],
                check=True,
            )
            os.makedirs(project)
            with open(tracked, "w", encoding="utf-8") as stream:
                stream.write("versioned")
            subprocess.run(
                [svn, "add", "--parents", tracked],
                check=True,
                capture_output=True,
            )

            with self.assertRaisesRegex(ValueError, "SVN 已版本化"):
                CompareToolApp._validate_repository_output_targets(
                    project, "svn", [tracked]
                )

    def test_svn_subdirectory_project_detects_versioned_sibling_target(self):
        svn = shutil.which("svn")
        svnadmin = shutil.which("svnadmin")
        if not svn or not svnadmin:
            self.skipTest("svn command line tools are not installed")
        with project_temp_dir() as root:
            repository = os.path.join(root, "repository")
            working_copy = os.path.join(root, "working-copy")
            project = os.path.join(working_copy, "component")
            sibling = os.path.join(working_copy, "delivery", "source.txt")
            subprocess.run([svnadmin, "create", repository], check=True)
            subprocess.run(
                [svn, "checkout", "-q", Path(repository).as_uri(), working_copy],
                check=True,
            )
            os.makedirs(project)
            os.makedirs(os.path.dirname(sibling))
            with open(sibling, "w", encoding="utf-8") as stream:
                stream.write("versioned sibling")
            subprocess.run(
                [svn, "add", "--parents", sibling],
                check=True,
                capture_output=True,
            )

            with self.assertRaisesRegex(ValueError, "SVN 已版本化"):
                CompareToolApp._validate_repository_output_targets(
                    project, "svn", [sibling]
                )

    def test_repository_metadata_directory_is_never_an_output_target(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "repo")
            target = os.path.join(source, ".git", "newVersion")
            os.makedirs(source)
            top = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=(os.path.abspath(source) + "\n").encode("utf-8"),
                stderr=b"",
            )
            with mock.patch(
                "main.GitVCS._find_git", return_value="git"
            ), mock.patch("main.subprocess.run", return_value=top):
                with self.assertRaisesRegex(ValueError, "元数据目录"):
                    CompareToolApp._validate_repository_output_targets(
                        source, "git", [target]
                    )

    def test_existing_output_may_contain_metadata_named_source_content(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "repo")
            target = os.path.join(source, "oldVersion", "Demo")
            nested_metadata = os.path.join(target, "vendor", ".git")
            os.makedirs(nested_metadata)
            top = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=(os.path.abspath(source) + "\n").encode("utf-8"),
                stderr=b"",
            )
            untracked = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            with mock.patch(
                "main.GitVCS._find_git", return_value="git"
            ), mock.patch(
                "main.subprocess.run", side_effect=[top, untracked]
            ):
                CompareToolApp._validate_repository_output_targets(
                    source, "git", [target]
                )

    def test_formal_output_may_replace_previously_exported_git_tree(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "repo")
            target = os.path.join(source, "oldVersion", "Demo")
            nested_metadata = os.path.join(target, "vendor", ".git")
            os.makedirs(os.path.join(nested_metadata, "objects"))
            with open(os.path.join(nested_metadata, "HEAD"), "w", encoding="utf-8") as f:
                f.write("ref: refs/heads/main\n")
            top = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=(os.path.abspath(source) + "\n").encode("utf-8"),
                stderr=b"",
            )
            untracked = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            with mock.patch(
                "main.GitVCS._find_git", return_value="git"
            ), mock.patch(
                "main.subprocess.run", side_effect=[top, untracked]
            ):
                CompareToolApp._validate_repository_output_targets(
                    source, "git", [target]
                )

    def test_file_target_inside_nested_repository_is_not_written(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git is not installed")
        with project_temp_dir() as root:
            repo = os.path.join(root, "repo")
            project = os.path.join(repo, "component")
            nested = os.path.join(repo, "vendor")
            target = os.path.join(nested, "report.html")
            os.makedirs(project)
            subprocess.run([git, "init", "-q", repo], check=True)
            subprocess.run([git, "init", "-q", nested], check=True)

            with self.assertRaisesRegex(ValueError, "独立 Git/SVN"):
                CompareToolApp._validate_repository_output_targets(
                    project, "git", [target]
                )

    def test_fixed_repository_endpoint_may_write_to_dedicated_child(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "repo")
            target = os.path.join(source, "compare-output")
            os.makedirs(source)

            with self.assertRaisesRegex(ValueError, "重叠"):
                CompareToolApp._validate_source_output_separation(
                    [source], [target]
                )

            CompareToolApp._validate_source_output_separation(
                [source], [target], allow_descendant_outputs=True
            )

    def test_fixed_repository_mode_still_rejects_output_over_source(self):
        with project_temp_dir() as root:
            source = os.path.join(root, "repo")
            os.makedirs(source)
            with self.assertRaisesRegex(ValueError, "重叠"):
                CompareToolApp._validate_source_output_separation(
                    [source], [root], allow_descendant_outputs=True
                )


    @unittest.skipUnless(os.name == "nt", "Windows drive-root behavior")
    def test_windows_drive_root_is_a_valid_common_output_root(self):
        self.assertEqual(
            os.path.abspath("D:\\"),
            FileExporter._output_root(["D:\\old", "D:\\new"]),
        )


if __name__ == "__main__":
    unittest.main()
