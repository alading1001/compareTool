import subprocess
import os
from typing import List

from .base import BaseVCS, ChangedFile, ChangeType


def _unescape_git_path(raw: str) -> str:
    """解码 Git 的 C 风格转义路径（core.quotepath 默认开启时中文等字符会被转义）
    例: \"\\347\\274\\226\\350\\257\\221.bat\" → 编译.bat
    """
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
        # 将 \nnn 八进制转义还原为字节再 UTF-8 解码
        result = []
        i = 0
        while i < len(raw):
            if raw[i] == '\\' and i + 3 < len(raw) and raw[i+1:i+4].isdigit():
                # 八进制转义：最多取3位八进制数字
                end = i + 1
                while end < len(raw) and end - i <= 3 and raw[end] in '01234567':
                    end += 1
                octal = raw[i+1:end]
                byte_val = int(octal, 8)
                result.append(byte_val)
                i = end
            else:
                result.append(ord(raw[i]))
                i += 1
        raw = bytes(result).decode("utf-8", errors="replace")
    return raw


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
            path = _unescape_git_path(parts[-1])

            change_map = {
                "A": ChangeType.ADDED,
                "M": ChangeType.MODIFIED,
                "D": ChangeType.DELETED,
            }

            if code.startswith("R"):
                old_path = _unescape_git_path(parts[1]) if len(parts) > 2 else ""
                files.append(ChangedFile(
                    path=path, change_type=ChangeType.RENAMED, old_path=old_path
                ))
            elif code in change_map:
                files.append(ChangedFile(path=path, change_type=change_map[code]))
        return self._filter_files(files)

    def get_file_content(self, version: str, file_path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "show", f"{version}:{file_path}"],
                cwd=self.project_path,
                capture_output=True,
                timeout=30
            )
            if result.returncode != 0:
                return ""
            data = result.stdout
            for enc in ("utf-8", "gbk"):
                try:
                    return data.decode(enc)
                except UnicodeDecodeError:
                    continue
            return data.decode("utf-8", errors="replace")
        except (subprocess.TimeoutExpired, RuntimeError):
            return ""

    def get_file_content_working(self, file_path: str) -> str:
        full_path = os.path.join(self.project_path, file_path)
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            return ""
        with open(full_path, "rb") as f:
            data = f.read()
        # 自动检测编码
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

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
