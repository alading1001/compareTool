import subprocess
import os
import shutil
from typing import List

from .base import BaseVCS, ChangedFile, ChangeType
from logger import info, warn


GIT_NOT_FOUND_MESSAGE = (
    "未找到 Git 命令行工具 git.exe。\n"
    "请安装 Git for Windows，并在安装时允许添加到 PATH；"
    "或确认 git.exe 位于 C:\\Program Files\\Git\\cmd\\git.exe 等常见安装目录。"
)


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

    def __init__(self, project_path: str):
        super().__init__(project_path)
        self._git = self._find_git()

    @staticmethod
    def _find_git() -> str:
        """自动探测 git 可执行文件路径"""
        found = shutil.which("git")
        if found:
            info(f"自动探测 git (PATH): {found}")
            return found

        if os.name == "nt":
            try:
                import winreg
                extra_paths = []
                for root, key in [
                    (winreg.HKEY_CURRENT_USER, "Environment"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                ]:
                    try:
                        with winreg.OpenKey(root, key) as regkey:
                            extra_paths.append(winreg.QueryValueEx(regkey, "Path")[0])
                    except OSError:
                        pass
                merged = os.environ.get("PATH", "") + ";" + ";".join(extra_paths)
                for p in merged.split(";"):
                    p = p.strip().strip('"')
                    candidate = os.path.join(p, "git.exe")
                    if os.path.isfile(candidate):
                        info(f"自动探测 git (注册表PATH): {candidate}")
                        return candidate
            except Exception:
                pass

            candidates = [
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "git.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "cmd", "git.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "git.exe"),
                os.path.join(os.environ.get("LocalAppData", ""), "Programs", "Git", "cmd", "git.exe"),
                os.path.join(os.environ.get("LocalAppData", ""), "Programs", "Git", "bin", "git.exe"),
            ]
            for candidate in candidates:
                if candidate and os.path.isfile(candidate):
                    info(f"自动探测 git (常见位置): {candidate}")
                    return candidate

        warn("未找到 git，回退使用 'git'")
        return "git"

    def _run(self, args: list) -> str:
        try:
            result = subprocess.run(
                [self._git] + args,
                cwd=self.project_path,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            raise RuntimeError(GIT_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            raise RuntimeError(f"Git命令失败: {' '.join(args)}\n{result.stderr}")
        return result.stdout

    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        output = self._run(["diff", "--name-status", "--find-renames", old_version, new_version])
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
                [self._git, "show", f"{version}:{file_path}"],
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
        except (subprocess.TimeoutExpired, RuntimeError, FileNotFoundError):
            return ""

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        try:
            result = subprocess.run(
                [self._git, "show", f"{version}:{file_path}"],
                cwd=self.project_path,
                capture_output=True,
                timeout=30
            )
            if result.returncode != 0:
                return None
            data = result.stdout
            if self._autocrlf_effective() and self._is_text_bytes(data):
                data = self._apply_crlf(data)
            return data
        except (subprocess.TimeoutExpired, RuntimeError, FileNotFoundError):
            return None

    def _autocrlf_effective(self) -> bool:
        """检查 core.autocrlf 是否为 true（缓存结果）"""
        if hasattr(self, '_cached_autocrlf'):
            return self._cached_autocrlf
        try:
            r = subprocess.run(
                [self._git, "config", "--get", "core.autocrlf"],
                cwd=self.project_path,
                capture_output=True, text=True, timeout=5
            )
            self._cached_autocrlf = r.stdout.strip().lower() == "true"
        except Exception:
            self._cached_autocrlf = False
        return self._cached_autocrlf

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

        # 3. 最近100条提交（用于同分支不同commit比对）
        try:
            commits = self._run(["log", "--oneline", "-100", "--format=%h %s"]).strip().split("\n")
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
        except RuntimeError as exc:
            if GIT_NOT_FOUND_MESSAGE in str(exc):
                raise
            return False
