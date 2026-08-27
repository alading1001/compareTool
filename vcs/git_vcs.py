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
        # Git 使用 C 风格转义：八进制序列表示 UTF-8 字节，
        # tab/换行/引号/反斜杠等使用单字符转义。
        result = bytearray()
        simple_escapes = {
            "a": 0x07,
            "b": 0x08,
            "t": 0x09,
            "n": 0x0A,
            "v": 0x0B,
            "f": 0x0C,
            "r": 0x0D,
            "\\": 0x5C,
            '"': 0x22,
        }
        i = 0
        while i < len(raw):
            if raw[i] == '\\' and i + 1 < len(raw) and raw[i + 1] in "01234567":
                end = i + 1
                while end < len(raw) and end - i <= 3 and raw[end] in '01234567':
                    end += 1
                result.append(int(raw[i + 1:end], 8))
                i = end
            elif raw[i] == '\\' and i + 1 < len(raw) and raw[i + 1] in simple_escapes:
                result.append(simple_escapes[raw[i + 1]])
                i += 2
            else:
                result.extend(raw[i].encode("utf-8"))
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

    def _run_bytes(self, args: list) -> bytes:
        try:
            result = subprocess.run(
                [self._git] + args,
                cwd=self.project_path,
                capture_output=True,
                timeout=600,
            )
        except FileNotFoundError:
            raise RuntimeError(GIT_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Git命令失败: {' '.join(args)}\n{stderr}")
        return result.stdout

    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        output = self._run_bytes([
            "diff", "--raw", "-z", "--find-renames", old_version, new_version, "--"
        ])
        fields = output.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        files = []
        index = 0
        while index < len(fields):
            header = fields[index]
            index += 1
            if not header.startswith(b":"):
                raise RuntimeError("无法解析 Git raw 变更记录")
            parts = header[1:].split()
            if len(parts) != 5:
                raise RuntimeError(
                    "无法解析 Git raw 变更头: "
                    + header.decode("utf-8", errors="replace")
                )
            old_mode = parts[0].decode("ascii", errors="replace")
            new_mode = parts[1].decode("ascii", errors="replace")
            status = parts[4].decode("ascii", errors="replace")
            code = status[:1]
            if code in ("R", "C"):
                if index + 1 >= len(fields):
                    raise RuntimeError("无法解析 Git 重命名/复制路径")
                old_path = fields[index].decode("utf-8", errors="surrogateescape")
                path = fields[index + 1].decode("utf-8", errors="surrogateescape")
                index += 2
            else:
                if index >= len(fields):
                    raise RuntimeError("无法解析 Git 变更路径")
                path = fields[index].decode("utf-8", errors="surrogateescape")
                old_path = ""
                index += 1

            if code == "T":
                raise RuntimeError(
                    f"Git 文件类型发生变化（如普通文件与符号链接互换），"
                    f"已中止生成以避免导出错误文件: {path}"
                )
            self._validate_regular_modes(old_mode, new_mode, path)
            metadata = self._mode_metadata(old_mode, new_mode)
            kwargs = {
                "metadata_changes": metadata,
                "old_executable": self._mode_executable(old_mode),
                "new_executable": self._mode_executable(new_mode),
                "old_mode": "" if old_mode == "000000" else old_mode,
                "new_mode": "" if new_mode == "000000" else new_mode,
            }

            if code == "R":
                files.append(ChangedFile(
                    path=path,
                    change_type=ChangeType.RENAMED,
                    old_path=old_path,
                    **kwargs,
                ))
            elif code == "C":
                files.append(ChangedFile(
                    path=path,
                    change_type=ChangeType.ADDED,
                    **kwargs,
                ))
            elif code in {"A", "M", "D"}:
                change_type = {
                    "A": ChangeType.ADDED,
                    "M": ChangeType.MODIFIED,
                    "D": ChangeType.DELETED,
                }[code]
                files.append(ChangedFile(path=path, change_type=change_type, **kwargs))
            else:
                raise RuntimeError(f"暂不支持的 Git 变更类型 {code}: {path}")
        return self._filter_files(files)

    @staticmethod
    def _mode_executable(mode: str):
        if mode == "000000":
            return None
        return mode == "100755"

    @staticmethod
    def _mode_metadata(old_mode: str, new_mode: str) -> List[str]:
        if old_mode in ("000000", new_mode) or new_mode == "000000":
            return []
        return [f"Git 文件模式：{old_mode} → {new_mode}"]

    @staticmethod
    def _validate_regular_modes(old_mode: str, new_mode: str, path: str):
        invalid = [
            mode for mode in (old_mode, new_mode)
            if mode not in ("000000", "100644", "100755")
        ]
        if invalid:
            raise RuntimeError(
                f"Git 端点不是普通文件，已中止生成: {path} "
                f"(mode={old_mode}->{new_mode})"
            )

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
        data = self.get_file_content_raw_bytes(version, file_path)
        if data is not None and self._checkout_uses_crlf(version, file_path, data):
            data = self._apply_crlf(data)
        return data

    def get_file_content_raw_bytes(self, version: str, file_path: str) -> bytes:
        """读取 Git 对象中的原始字节，不应用工作副本换行符转换。"""
        try:
            result = subprocess.run(
                [self._git, "show", f"{version}:{file_path}"],
                cwd=self.project_path,
                capture_output=True,
                timeout=30
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, RuntimeError, FileNotFoundError):
            return None

    def _autocrlf_effective(self) -> bool:
        """检查 core.autocrlf 是否为 true（缓存结果）"""
        return self._git_config_value("core.autocrlf") == "true"

    def _git_config_value(self, name: str) -> str:
        cache = getattr(self, "_config_cache", None)
        if cache is None:
            self._config_cache = {}
            cache = self._config_cache
        if name in cache:
            return cache[name]
        try:
            r = subprocess.run(
                [self._git, "config", "--get", name],
                cwd=self.project_path,
                capture_output=True, text=True, timeout=5
            )
            value = r.stdout.strip().lower() if r.returncode == 0 else ""
        except Exception:
            value = ""
        cache[name] = value
        return value

    @staticmethod
    def _parse_check_attr_output(data: bytes) -> dict:
        parts = data.split(b"\x00")
        attrs = {}
        for index in range(0, len(parts) - 2, 3):
            name = parts[index + 1].decode("utf-8", errors="replace")
            value = parts[index + 2].decode("utf-8", errors="replace")
            attrs[name] = value.lower()
        return attrs

    def _get_checkout_attributes(self, version: str, file_path: str) -> dict:
        cache = getattr(self, "_attribute_cache", None)
        if cache is None:
            self._attribute_cache = {}
            cache = self._attribute_cache
        cache_key = (version, file_path)
        if cache_key in cache:
            return cache[cache_key]

        result = subprocess.run(
            [self._git, "check-attr", "-z", f"--source={version}",
             "text", "eol", "--", file_path],
            cwd=self.project_path,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"无法读取 Git 属性，已中止导出: {file_path}\n{stderr.strip()}"
            )
        attrs = self._parse_check_attr_output(result.stdout)
        cache[cache_key] = attrs
        return attrs

    def _checkout_uses_crlf(self, version: str, file_path: str, data: bytes) -> bool:
        if not self._is_text_bytes(data):
            return False

        attrs = self._get_checkout_attributes(version, file_path)
        text_attr = attrs.get("text", "unspecified")
        eol_attr = attrs.get("eol", "unspecified")
        if text_attr == "unset":
            return False
        if eol_attr == "lf":
            return False
        if eol_attr == "crlf":
            return True

        autocrlf = self._git_config_value("core.autocrlf")
        if autocrlf == "true":
            return True
        if autocrlf == "input":
            return False

        if text_attr in ("set", "auto"):
            core_eol = self._git_config_value("core.eol")
            if core_eol == "crlf":
                return True
            if core_eol == "native":
                return os.linesep == "\r\n"
        return False

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
