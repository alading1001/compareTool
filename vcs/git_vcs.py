import subprocess
import os
from typing import List

from .base import BaseVCS, ChangedFile, ChangeType


class GitVCS(BaseVCS):
    """Git版本控制实现"""

    def _run(self, args: list) -> str:
        result = subprocess.run(
            ["git"] + args,
            cwd=self.project_path,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git命令失败: {' '.join(args)}\n{result.stderr}")
        return result.stdout

    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        output = self._run(["diff", "--name-status", old_version, new_version])
        files = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            code = parts[0]
            path = parts[-1]

            change_map = {
                "A": ChangeType.ADDED,
                "M": ChangeType.MODIFIED,
                "D": ChangeType.DELETED,
            }

            if code.startswith("R"):
                old_path = parts[1] if len(parts) > 2 else ""
                files.append(ChangedFile(
                    path=path, change_type=ChangeType.RENAMED, old_path=old_path
                ))
            elif code in change_map:
                files.append(ChangedFile(path=path, change_type=change_map[code]))
        return self._filter_files(files)

    def get_file_content(self, version: str, file_path: str) -> str:
        try:
            return self._run(["show", f"{version}:{file_path}"])
        except RuntimeError:
            # 文件在该版本不存在（新增文件在旧版本中）
            return ""

    def get_file_content_working(self, file_path: str) -> str:
        full_path = os.path.join(self.project_path, file_path)
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            return ""
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def get_versions(self) -> List[str]:
        result = []
        seen = set()

        # 1. Tags
        tags = self._run(["tag", "--sort=-creatordate"]).strip().split("\n")
        if tags and tags[0]:
            result.append("── Tags ──")
            for t in tags:
                if t and t not in seen:
                    result.append(t)
                    seen.add(t)

        # 2. 本地分支
        branches = self._run(["branch", "--sort=-committerdate"]).strip().split("\n")
        branches = [b.strip().lstrip("* ") for b in branches if b.strip()]
        if branches:
            result.append("── 分支 ──")
            for b in branches:
                if b and b not in seen and not b.startswith("remotes/"):
                    result.append(b)
                    seen.add(b)

        # 3. 最近50条提交（用于同分支不同commit比对）
        try:
            commits = self._run(["log", "--oneline", "-50", "--format=%h %s"]).strip().split("\n")
            if commits and commits[0]:
                result.append("── 最近提交记录 ──")
                for c in commits:
                    if c.strip():
                        result.append(c.strip())
        except RuntimeError:
            pass

        return result

    def check_version_exists(self, version: str) -> bool:
        try:
            self._run(["rev-parse", "--verify", f"{version}^{{commit}}"])
            return True
        except RuntimeError:
            return False
