import os
import re
import shutil
import stat
import subprocess
import tempfile
from typing import List

from .base import BaseVCS, ChangedFile
from .folder_vcs import FolderVCS
from .git_vcs import GitVCS, GIT_NOT_FOUND_MESSAGE
from .svn_vcs import SVNVCS, SVN_NOT_FOUND_MESSAGE


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

    def get_file_content_raw_bytes(self, version: str, file_path: str) -> bytes:
        return self._folder.get_file_content_raw_bytes(self._to_folder_ver(version), file_path)

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
    """Git 需求包比对：从最早选中提交的第一父提交开始，只应用选中提交。"""

    def __init__(self, project_path: str, selected_versions: List[str]):
        self._git_exe = GitVCS._find_git()
        super().__init__(project_path, selected_versions, "comparetool_git_multi_")
        try:
            self._prepare()
            self._init_folder_delegate()
        except Exception:
            self.cleanup()
            raise

    @staticmethod
    def _run(args: list, cwd: str, input_bytes: bytes = None) -> bytes:
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                input=input_bytes,
                capture_output=True,
                timeout=600
            )
        except FileNotFoundError:
            if args and os.path.basename(args[0]).lower() in ("git", "git.exe"):
                raise RuntimeError(GIT_NOT_FOUND_MESSAGE)
            raise
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            stdout = result.stdout.decode("utf-8", errors="replace")
            raise RuntimeError(f"Git命令失败: {' '.join(args)}\n{stderr or stdout}")
        return result.stdout

    @staticmethod
    def get_recent_versions(project_path: str, limit: int = 100) -> List[str]:
        git = GitVCS._find_git()
        try:
            result = subprocess.run(
                [git, "log", "--first-parent", f"-{limit}", "--format=%h%x09%p%x09%s"],
                cwd=project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30
            )
        except FileNotFoundError:
            raise RuntimeError(GIT_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            raise RuntimeError(f"Git命令失败: git log\n{result.stderr}")
        versions = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            short_hash = parts[0].strip()
            parents = parts[1].split() if len(parts) > 1 else []
            subject = parts[2].strip() if len(parts) > 2 else ""
            marker = " [merge]" if len(parents) > 1 else ""
            versions.append(f"{short_hash}{marker} {subject}".strip())
        return versions

    def _git(self, *args: str, cwd: str = None) -> str:
        out = self._run([self._git_exe] + list(args), cwd or self.source_project_path)
        return out.decode("utf-8", errors="replace").strip()

    def _prepare(self):
        resolved = self._resolve_commits()
        ordered = self._sort_oldest_first(resolved)
        base_commit = self._parent_commit(ordered[0])
        if not base_commit:
            raise RuntimeError("暂不支持选择 Git 根提交作为多版本比对起点")

        work_dir = os.path.join(self._tmp_root, "work")
        self._run(
            [self._git_exe, "clone", "--local", "--no-hardlinks", self.source_project_path, work_dir],
            cwd=self.source_project_path
        )
        self._run([self._git_exe, "checkout", "--detach", base_commit], cwd=work_dir)

        _copy_snapshot(work_dir, self._old_dir)

        for commit in ordered:
            result = subprocess.run(
                self._cherry_pick_args(commit),
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
                subprocess.run(
                    [self._git_exe, "cherry-pick", "--abort"],
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace"
                )
                raise RuntimeError(
                    "应用选中提交时发生冲突或失败，已取消生成。\n"
                    f"提交: {commit}\n"
                    f"{detail}"
                )

        _copy_snapshot(work_dir, self._new_dir)

    def _resolve_commits(self) -> List[str]:
        commits = []
        for raw in self.selected_versions:
            commit = self._git("rev-parse", "--verify", f"{raw}^{{commit}}")
            if not commit:
                raise RuntimeError(f"提交不存在: {raw}")

            is_ancestor = subprocess.run(
                [self._git_exe, "merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=self.source_project_path,
                capture_output=True
            )
            if is_ancestor.returncode != 0:
                raise RuntimeError(f"提交不在当前分支 HEAD 历史中: {raw}")

            commits.append(commit)
        return commits

    def _sort_oldest_first(self, commits: List[str]) -> List[str]:
        history = self._git("rev-list", "--topo-order", "HEAD").splitlines()
        order = {commit: idx for idx, commit in enumerate(history)}
        return sorted(commits, key=lambda c: order.get(c, -1), reverse=True)

    def _parent_commit(self, commit: str) -> str:
        parents = self._parents(commit)
        return parents[0] if parents else ""

    def _parents(self, commit: str) -> List[str]:
        parts = self._git("rev-list", "--parents", "-n", "1", commit).split()
        return parts[1:]

    def _is_merge_commit(self, commit: str) -> bool:
        return len(self._parents(commit)) > 1

    def _cherry_pick_args(self, commit: str) -> List[str]:
        args = [self._git_exe, "cherry-pick", "--no-commit"]
        if self._is_merge_commit(commit):
            args.extend(["-m", "1"])
        args.append(commit)
        return args

    def _conflict_files(self, work_dir: str) -> List[str]:
        result = subprocess.run(
            [self._git_exe, "diff", "--name-only", "--diff-filter=U"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class SVNMultiVersionVCS(_MultiVersionFolderDelegate):
    """SVN 多版本比对：从最早选中 revision 的前一版开始，只应用选中 revision。"""

    @staticmethod
    def get_recent_versions(project_path: str, svn_path: str = "", limit: int = 100) -> List[str]:
        svn = svn_path or SVNVCS._find_svn()
        try:
            info = subprocess.run(
                [svn, "info", "--non-interactive", "--show-item", "url"],
                cwd=project_path,
                capture_output=True,
                timeout=60
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        if info.returncode != 0:
            stderr = SVNMultiVersionVCS._decode(info.stderr) if info.stderr else ""
            stdout = SVNMultiVersionVCS._decode(info.stdout) if info.stdout else ""
            raise RuntimeError(f"SVN命令失败: info\n{stderr or stdout}")
        url = SVNMultiVersionVCS._decode(info.stdout).strip()
        try:
            result = subprocess.run(
                [svn, "log", "-r", "HEAD:1", "-l", str(limit), "--non-interactive", f"{url}@HEAD"],
                capture_output=True,
                timeout=60
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
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
        try:
            result = subprocess.run(
                full_cmd,
                cwd=cwd or self.source_project_path,
                capture_output=True,
                timeout=600
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
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
        revisions.sort()

        url = self._run(["info", "--non-interactive", "--show-item", "url"]).strip()
        if not url:
            raise RuntimeError("无法获取 SVN 仓库 URL")

        base_rev = revisions[0] - 1
        work_dir = os.path.join(self._tmp_root, "work")

        if self._url_exists(url, base_rev):
            self._run(["export", "--non-interactive", "-r", str(base_rev), url, self._old_dir])
            self._run(["checkout", "--non-interactive", "-r", str(base_rev), url, work_dir])
            apply_revisions = revisions
        else:
            os.makedirs(self._old_dir, exist_ok=True)
            if not self._url_exists(url, revisions[0]):
                raise RuntimeError(f"SVN 路径在基线版本 r{base_rev} 和首个选中版本 r{revisions[0]} 均不存在")
            self._run(["checkout", "--non-interactive", "-r", str(revisions[0]), url, work_dir])
            apply_revisions = revisions[1:]

        for rev in apply_revisions:
            try:
                result = subprocess.run(
                    [self._svn, "merge", "--non-interactive", "--accept", "postpone",
                     "-c", str(rev), f"{url}@HEAD", work_dir],
                    cwd=work_dir,
                    capture_output=True,
                    timeout=600
                )
            except FileNotFoundError:
                raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
            conflicts = self._conflict_files(work_dir)
            if result.returncode != 0 or conflicts:
                stderr = self._decode(result.stderr) if result.stderr else ""
                stdout = self._decode(result.stdout) if result.stdout else ""
                detail = "\n".join(conflicts) if conflicts else (stderr or stdout)
                raise RuntimeError(
                    "应用选中 SVN 修订时发生冲突或失败，已取消生成。\n"
                    f"Revision: r{rev}\n"
                    f"{detail}"
                )

        _copy_snapshot(work_dir, self._new_dir)

    def _url_exists(self, url: str, rev: int) -> bool:
        try:
            result = subprocess.run(
                [self._svn, "info", "--non-interactive", "--show-item", "kind", f"{url}@{rev}"],
                cwd=self.source_project_path,
                capture_output=True,
                timeout=60
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        return result.returncode == 0

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
