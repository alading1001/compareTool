import os
import re
import shutil
import stat
import subprocess
import tempfile
from typing import List

from .base import BaseVCS, ChangedFile
from .folder_vcs import FolderVCS
from .svn_vcs import SVNVCS


def parse_multi_versions(raw: str) -> List[str]:
    """解析用户输入的多个版本号，支持逗号、分号、换行分隔。"""
    result = []
    seen = set()
    for part in re.split(r"[,;\r\n]+", raw or ""):
        token = part.strip().split()[0] if part.strip() else ""
        if token and token not in seen:
            result.append(token)
            seen.add(token)
    return result


def _copy_snapshot(src: str, dst: str):
    """复制工作目录快照，跳过 VCS 元数据。"""
    def ignore(dir_path, names):
        return {n for n in names if n in {".git", ".svn"}}

    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)


def _remove_tree(path: str):
    """Windows 上 SVN/Git 元数据可能只读，删除失败时先恢复写权限。"""
    def onerror(func, failed_path, exc_info):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except Exception:
            pass

    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)


class _MultiVersionFolderDelegate(BaseVCS):
    """多版本比对公共委托：最终仍复用 FolderVCS。"""

    def __init__(self, source_project_path: str, selected_versions: List[str], prefix: str):
        if not selected_versions:
            raise ValueError("请选择至少一个版本")
        self.source_project_path = source_project_path
        self.selected_versions = selected_versions
        self.old_version_label = ", ".join(selected_versions)
        self._tmp_root = tempfile.mkdtemp(prefix=prefix)
        self._old_dir = os.path.join(self._tmp_root, "old")
        self._new_dir = os.path.join(self._tmp_root, "new")
        self._folder = None
        super().__init__(self._new_dir)

    def _init_folder_delegate(self):
        self._folder = FolderVCS(self._old_dir, self._new_dir)

    def set_exclude_patterns(self, patterns: List[str]):
        super().set_exclude_patterns(patterns)
        if self._folder:
            self._folder.set_exclude_patterns(patterns)

    def _to_folder_ver(self, version: str) -> str:
        if version in ("old", self.old_version_label):
            return "old"
        return "new"

    def get_changed_files(self, old_version: str = "", new_version: str = "") -> List[ChangedFile]:
        return self._folder.get_changed_files("old", "new")

    def get_file_content(self, version: str, file_path: str) -> str:
        return self._folder.get_file_content(self._to_folder_ver(version), file_path)

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        return self._folder.get_file_content_bytes(self._to_folder_ver(version), file_path)

    def get_file_content_working(self, file_path: str) -> str:
        return self._folder.get_file_content_working(file_path)

    def get_file_content_bytes_working(self, file_path: str) -> bytes:
        return self._folder.get_file_content_bytes_working(file_path)

    def get_versions(self) -> List[str]:
        return []

    def check_version_exists(self, version: str) -> bool:
        return True

    def cleanup(self):
        try:
            _remove_tree(self._tmp_root)
        except Exception:
            pass

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass


class GitMultiVersionVCS(_MultiVersionFolderDelegate):
    """Git 多版本比对：基于 HEAD 干净副本，反向撤销选中提交。"""

    def __init__(self, project_path: str, selected_versions: List[str]):
        super().__init__(project_path, selected_versions, "comparetool_git_multi_")
        try:
            self._prepare()
            self._init_folder_delegate()
        except Exception:
            self.cleanup()
            raise

    @staticmethod
    def _run(args: list, cwd: str, input_bytes: bytes = None) -> bytes:
        result = subprocess.run(
            args,
            cwd=cwd,
            input=input_bytes,
            capture_output=True,
            timeout=600
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            stdout = result.stdout.decode("utf-8", errors="replace")
            raise RuntimeError(f"Git命令失败: {' '.join(args)}\n{stderr or stdout}")
        return result.stdout

    @staticmethod
    def get_recent_versions(project_path: str, limit: int = 100) -> List[str]:
        result = subprocess.run(
            ["git", "log", "--first-parent", "--no-merges", f"-{limit}", "--format=%h %s"],
            cwd=project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git命令失败: git log\n{result.stderr}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _git(self, *args: str, cwd: str = None) -> str:
        out = self._run(["git"] + list(args), cwd or self.source_project_path)
        return out.decode("utf-8", errors="replace").strip()

    def _prepare(self):
        resolved = self._resolve_commits()
        ordered = self._sort_newest_first(resolved)

        work_dir = os.path.join(self._tmp_root, "work")
        self._run(
            ["git", "clone", "--local", "--no-hardlinks", self.source_project_path, work_dir],
            cwd=self.source_project_path
        )
        self._run(["git", "checkout", "--detach", "HEAD"], cwd=work_dir)

        _copy_snapshot(work_dir, self._new_dir)

        for commit in ordered:
            result = subprocess.run(
                ["git", "revert", "--no-commit", commit],
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600
            )
            if result.returncode != 0:
                conflicts = self._conflict_files(work_dir)
                detail = "\n".join(conflicts) if conflicts else (result.stderr or result.stdout)
                raise RuntimeError(
                    "反向回滚选中提交时发生冲突或失败，已取消生成。\n"
                    f"提交: {commit}\n"
                    f"{detail}"
                )

        _copy_snapshot(work_dir, self._old_dir)

    def _resolve_commits(self) -> List[str]:
        commits = []
        for raw in self.selected_versions:
            commit = self._git("rev-parse", "--verify", f"{raw}^{{commit}}")
            if not commit:
                raise RuntimeError(f"提交不存在: {raw}")

            is_ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=self.source_project_path,
                capture_output=True
            )
            if is_ancestor.returncode != 0:
                raise RuntimeError(f"提交不在当前分支 HEAD 历史中: {raw}")

            parents_line = self._git("rev-list", "--parents", "-n", "1", commit)
            if len(parents_line.split()) > 2:
                raise RuntimeError(f"暂不支持 Git 合并提交: {raw}")

            commits.append(commit)
        return commits

    def _sort_newest_first(self, commits: List[str]) -> List[str]:
        history = self._git("rev-list", "--topo-order", "HEAD").splitlines()
        order = {commit: idx for idx, commit in enumerate(history)}
        return sorted(commits, key=lambda c: order.get(c, 10 ** 9))

    @staticmethod
    def _conflict_files(work_dir: str) -> List[str]:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class SVNMultiVersionVCS(_MultiVersionFolderDelegate):
    """SVN 多版本比对：基于仓库 HEAD，反向 merge 选中 revision。"""

    @staticmethod
    def get_recent_versions(project_path: str, svn_path: str = "", limit: int = 100) -> List[str]:
        svn = svn_path or SVNVCS._find_svn()
        result = subprocess.run(
            [svn, "log", "-r", "HEAD:1", "-l", str(limit), "--non-interactive", project_path],
            capture_output=True,
            timeout=60
        )
        if result.returncode != 0:
            stderr = SVNMultiVersionVCS._decode(result.stderr) if result.stderr else ""
            stdout = SVNMultiVersionVCS._decode(result.stdout) if result.stdout else ""
            raise RuntimeError(f"SVN命令失败: log\n{stderr or stdout}")
        return SVNMultiVersionVCS._parse_log_versions(SVNMultiVersionVCS._decode(result.stdout))

    @staticmethod
    def _parse_log_versions(output: str) -> List[str]:
        revisions = []
        entries = output.split("------------------------------------------------------------------------")
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            lines = entry.split("\n")
            first = lines[0].strip()
            m = re.match(r'^r(\d+) \|', first)
            if not m:
                continue
            rev = f"r{m.group(1)}"
            msg_parts = []
            in_paths = False
            for line in lines[1:]:
                s = line.strip()
                if not s:
                    in_paths = False
                    continue
                if s.startswith("Changed paths:"):
                    in_paths = True
                    continue
                if in_paths:
                    continue
                msg_parts.append(s)
            msg = " ".join(msg_parts)
            revisions.append(f"{rev} {msg[:60]}{'...' if len(msg) > 60 else ''}" if msg else rev)
        return revisions

    def __init__(self, project_path: str, selected_versions: List[str], svn_path: str = ""):
        self._svn = svn_path or SVNVCS._find_svn()
        super().__init__(project_path, selected_versions, "comparetool_svn_multi_")
        try:
            self._prepare()
            self._init_folder_delegate()
        except Exception:
            self.cleanup()
            raise

    def _run(self, args: list, cwd: str = None) -> str:
        full_cmd = [self._svn] + args
        result = subprocess.run(
            full_cmd,
            cwd=cwd or self.source_project_path,
            capture_output=True,
            timeout=600
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            raise RuntimeError(f"SVN命令失败: {' '.join(args)}\n{stderr or stdout}")
        return self._decode(result.stdout)

    @staticmethod
    def _decode(data: bytes) -> str:
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def _prepare(self):
        revisions = self._parse_revisions()
        revisions.sort(reverse=True)

        url = self._run(["info", "--non-interactive", "--show-item", "url"]).strip()
        if not url:
            raise RuntimeError("无法获取 SVN 仓库 URL")

        work_dir = os.path.join(self._tmp_root, "work")
        self._run(["export", "--non-interactive", "-r", "HEAD", url, self._new_dir])
        self._run(["checkout", "--non-interactive", "-r", "HEAD", url, work_dir])

        for rev in revisions:
            result = subprocess.run(
                [self._svn, "merge", "--non-interactive", "--accept", "postpone", "-c", f"-{rev}", url, work_dir],
                cwd=work_dir,
                capture_output=True,
                timeout=600
            )
            conflicts = self._conflict_files(work_dir)
            if result.returncode != 0 or conflicts:
                stderr = self._decode(result.stderr) if result.stderr else ""
                stdout = self._decode(result.stdout) if result.stdout else ""
                detail = "\n".join(conflicts) if conflicts else (stderr or stdout)
                raise RuntimeError(
                    "反向回滚选中 SVN 修订时发生冲突或失败，已取消生成。\n"
                    f"Revision: r{rev}\n"
                    f"{detail}"
                )

        _copy_snapshot(work_dir, self._old_dir)

    def _parse_revisions(self) -> List[int]:
        revisions = []
        for raw in self.selected_versions:
            token = raw.strip().split()[0].lstrip("rR")
            if not token.isdigit():
                raise RuntimeError(f"SVN revision 格式不正确: {raw}")
            revisions.append(int(token))
        return revisions

    def _conflict_files(self, work_dir: str) -> List[str]:
        try:
            output = self._run(["status"], cwd=work_dir)
        except RuntimeError:
            return []
        conflicts = []
        for line in output.splitlines():
            if line and line[0] == "C":
                conflicts.append(line[1:].strip())
        return conflicts
