from task_progress import measured_phase
import subprocess
import os
import re
import shutil
from typing import List

from .base import BaseVCS, ChangedFile, ChangeType
from .git_checkout import GitCheckoutSnapshot
from .git_batch import GitBatchReader
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

    # 生成任务默认不设命令超时；用户可以等待慢仓库/慢网络。
    COMMAND_TIMEOUT = None

    def __init__(self, project_path: str):
        super().__init__(project_path)
        self._git = self._find_git()
        self._version_pins = {}
        self._checkout_snapshot = None
        self._batch_reader = None

    def _git_cwd(self):
        if not hasattr(self, "_repository_root"):
            result = subprocess.run(
                [self._git, "rev-parse", "--show-toplevel", "--show-prefix"],
                cwd=self.project_path, capture_output=True,
                timeout=self.COMMAND_TIMEOUT,
            )
            if result.returncode:
                bare = subprocess.run(
                    [self._git, "rev-parse", "--is-bare-repository", "--absolute-git-dir"],
                    cwd=self.project_path, capture_output=True, timeout=self.COMMAND_TIMEOUT,
                )
                lines = bare.stdout.decode("utf-8", "surrogateescape").splitlines()
                if bare.returncode == 0 and len(lines) == 2 and lines[0] == "true":
                    self._repository_root, self._project_prefix = lines[1], ""
                    return self._repository_root
                raise RuntimeError("无法确定 Git 项目范围：\n" + result.stderr.decode("utf-8", "replace"))
            lines = result.stdout.decode("utf-8", "surrogateescape").splitlines()
            self._repository_root = lines[0]
            self._project_prefix = lines[1] if len(lines) > 1 else ""
        return self._repository_root

    def _repo_path(self, project_path):
        self._git_cwd()
        return self._project_prefix + project_path.replace("\\", "/")

    def _relative_project_path(self, repository_path):
        self._git_cwd()
        if not repository_path.startswith(self._project_prefix):
            return None
        return repository_path[len(self._project_prefix):]

    def _get_batch_reader(self):
        reader = getattr(self, "_batch_reader", None)
        if reader is None:
            reader = GitBatchReader(self._git, self._git_cwd())
            self._batch_reader = reader
        return reader

    def cleanup(self):
        reader = getattr(self, "_batch_reader", None)
        if reader is not None:
            reader.close()
            self._batch_reader = None
        snapshot = getattr(self, "_checkout_snapshot", None)
        if snapshot is not None:
            snapshot.cleanup()
            self._checkout_snapshot = None

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

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
                cwd=self._git_cwd(),
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
                cwd=self._git_cwd(),
                capture_output=True,
                timeout=self.COMMAND_TIMEOUT,
            )
        except FileNotFoundError:
            raise RuntimeError(GIT_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Git命令失败: {' '.join(args)}\n{stderr}")
        return result.stdout

    @measured_phase('git.changes', '查询 Git 版本差异')
    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        pinned = self._pin_versions_stable((old_version, new_version))
        old_endpoint = pinned[str(old_version)]
        new_endpoint = pinned[str(new_version)]
        self._snapshot_git_config()
        output = self._run_bytes([
            "diff", "--raw", "-z", "--find-renames", old_endpoint, new_endpoint, "--"
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

            # cwd 不会限制 Git diff 的范围；这里统一改为项目相对路径。
            path = self._relative_project_path(path)
            if code in ("R", "C"):
                old_path = self._relative_project_path(old_path)
                if code == "R" and path is None and old_path is not None:
                    code, path, old_path, new_mode = "D", old_path, "", "000000"
                elif path is None:
                    continue
                elif old_path is None:
                    code, old_path, old_mode = "A", "", "000000"
            elif path is None:
                continue

            old_excluded = False
            new_excluded = False
            if code == "R":
                old_excluded = self._is_excluded(old_path)
                new_excluded = self._is_excluded(path)
                if old_excluded and new_excluded:
                    continue
            elif code == "C":
                if self._is_excluded(path):
                    continue
                old_excluded = True
            elif self._is_excluded(path):
                continue
            else:
                old_excluded = code == "A"
                new_excluded = code == "D"

            if code == "T":
                raise RuntimeError(
                    f"Git 文件类型发生变化（如普通文件与符号链接互换），"
                    f"已中止生成以避免导出错误文件: {path}"
                )
            effective_old_mode = "000000" if old_excluded else old_mode
            effective_new_mode = "000000" if new_excluded else new_mode
            self._validate_regular_modes(
                effective_old_mode, effective_new_mode, path
            )
            metadata = self._mode_metadata(
                effective_old_mode, effective_new_mode
            )
            kwargs = {
                "metadata_changes": metadata,
                "old_executable": self._mode_executable(effective_old_mode),
                "new_executable": self._mode_executable(effective_new_mode),
                "old_mode": "" if effective_old_mode == "000000" else effective_old_mode,
                "new_mode": "" if effective_new_mode == "000000" else effective_new_mode,
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
        files = self._filter_files(files)
        checkout_endpoints = []
        for item in files:
            if item.change_type != ChangeType.ADDED:
                checkout_endpoints.append((
                    old_endpoint, item.old_path or item.path
                ))
            if item.change_type != ChangeType.DELETED:
                checkout_endpoints.append((new_endpoint, item.path))
        self._snapshot_checkout_policy(checkout_endpoints)
        return files

    def _pin_version(self, version: str) -> str:
        """把可变分支/标签固定到本次任务开始时的 commit。"""
        key = str(version)
        pins = getattr(self, "_version_pins", None)
        if pins is None:
            self._version_pins = {}
            pins = self._version_pins
        if key in pins:
            return pins[key]
        resolved = self._resolve_ref_once(key)
        pins[key] = resolved
        return resolved

    def _resolve_ref_once(self, version: str) -> str:
        try:
            resolved = self._run_bytes([
                "rev-parse", "--verify", f"{version}^{{commit}}"
            ]).decode("ascii", errors="strict").strip()
        except (RuntimeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"无法固定 Git 版本端点，已中止生成: {version}"
            ) from exc
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", resolved):
            raise RuntimeError(f"Git 版本端点解析结果无效: {version} -> {resolved}")
        return resolved

    def _pin_versions_stable(self, versions) -> dict:
        """成组复核所有 ref，避免取得跨 ref transaction 的混合快照。"""
        keys = list(dict.fromkeys(str(version) for version in versions))
        pins = getattr(self, "_version_pins", None)
        if pins is not None and all(key in pins for key in keys):
            return {key: pins[key] for key in keys}
        first = {key: self._resolve_ref_once(key) for key in keys}
        second = {key: self._resolve_ref_once(key) for key in keys}
        if first != second:
            raise RuntimeError("Git 版本引用在任务快照期间发生变化，已中止生成")
        pins = getattr(self, "_version_pins", None)
        if pins is None:
            self._version_pins = {}
            pins = self._version_pins
        pins.update(first)
        return first

    def _resolve_version(self, version: str) -> str:
        return getattr(self, "_version_pins", {}).get(str(version), str(version))

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
                [self._git, "show", f"{self._resolve_version(version)}:{self._repo_path(file_path)}"],
                cwd=self._git_cwd(),
                capture_output=True,
                timeout=self.COMMAND_TIMEOUT
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
        endpoint = self._resolve_version(version)
        attrs = self._get_checkout_attributes(version, file_path)
        self._validate_checkout_attributes(file_path, attrs)
        self._ensure_checkout_snapshot([(endpoint, file_path)])
        mode = self._checkout_snapshot.conversion_mode(
            endpoint, self._repo_path(file_path), self._filter_config_cache
        )
        data = self.get_file_content_raw_bytes(version, file_path)
        if data is not None and (mode == "text" or (
            mode == "auto" and GitCheckoutSnapshot.auto_text_uses_crlf([data])
        )):
            return self._apply_crlf(data)
        return data

    def get_file_content_raw_bytes(self, version: str, file_path: str) -> bytes:
        """Read original blob bytes, reusing a task-local batch process."""
        try:
            expression = f"{self._resolve_version(version)}:{self._repo_path(file_path)}"
            if self.COMMAND_TIMEOUT is None and GitBatchReader.supports(expression):
                return self._get_batch_reader().read_bytes(expression)
            result = subprocess.run(
                [self._git, "show", f"{self._resolve_version(version)}:{self._repo_path(file_path)}"],
                cwd=self._git_cwd(),
                capture_output=True,
                timeout=self.COMMAND_TIMEOUT
            )
            if result.returncode != 0:
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, RuntimeError, FileNotFoundError):
            return None

    def get_file_size(self, version: str, file_path: str):
        endpoint = self._resolve_version(version)
        try:
            output = self._run_bytes(["cat-file", "-s", f"{endpoint}:{self._repo_path(file_path)}"])
            value = output.decode("ascii", errors="strict").strip()
        except (RuntimeError, UnicodeDecodeError):
            return None
        return int(value) if value.isdigit() else None

    def get_file_signature(self, version: str, file_path: str):
        endpoint = self._resolve_version(version)
        try:
            object_id = self._run_bytes([
                "rev-parse", "--verify", f"{endpoint}:{self._repo_path(file_path)}"
            ]).decode("ascii", errors="strict").strip().lower()
        except (RuntimeError, UnicodeDecodeError):
            return None
        if not re.fullmatch(r"[0-9a-f]{40,64}", object_id):
            return None
        # Git blob OID 已绑定对象类型、完整长度和完整内容；再执行一次
        # cat-file -s 不增加签名判定能力，只会让每个重命名候选多一次子进程。
        return "git-object", object_id

    def export_file_to_path(self, version: str, file_path: str, target_path: str):
        endpoint = self._resolve_version(version)
        attrs = self._get_checkout_attributes(endpoint, file_path)
        self._validate_checkout_attributes(file_path, attrs)
        self._ensure_checkout_snapshot([(endpoint, file_path)])
        mode = self._checkout_snapshot.conversion_mode(
            endpoint, self._repo_path(file_path), self._filter_config_cache
        )
        self.export_raw_file_to_path(endpoint, file_path, target_path)
        convert = mode == "text"
        if mode == "auto":
            with open(target_path, "rb") as source:
                convert = GitCheckoutSnapshot.auto_text_uses_crlf(
                    iter(lambda: source.read(1024 * 1024), b"")
                )
        if convert:
            self._rewrite_file_lf_to_crlf(target_path)

    def export_raw_file_to_path(self, version: str, file_path: str, target_path: str):
        endpoint = self._resolve_version(version)
        expression = f"{endpoint}:{self._repo_path(file_path)}"
        try:
            with open(target_path, "wb") as target:
                if self.COMMAND_TIMEOUT is None and GitBatchReader.supports(expression):
                    self._get_batch_reader().copy_to(expression, target)
                    return
                result = subprocess.run(
                    [self._git, "show", f"{endpoint}:{self._repo_path(file_path)}"],
                    cwd=self._git_cwd(),
                    stdout=target,
                    stderr=subprocess.PIPE,
                    timeout=self.COMMAND_TIMEOUT,
                )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"无法流式导出 Git 文件: {file_path}\n{exc}") from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"无法流式导出 Git 文件: {file_path}\n"
                + result.stderr.decode("utf-8", errors="replace")
            )

    def _git_config_value(self, name: str) -> str:
        cache = getattr(self, "_config_cache", None)
        if cache is None:
            self._config_cache = {}
            cache = self._config_cache
        if name in cache:
            return cache[name]
        value = self._read_git_config_value(name)
        cache[name] = value
        return value

    def _read_git_config_value(self, name: str) -> str:
        try:
            r = subprocess.run(
                [self._git, "config", "--get", name],
                cwd=self._git_cwd(),
                capture_output=True, text=True, timeout=self.COMMAND_TIMEOUT
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(f"无法读取 Git 配置 {name}，已中止导出: {exc}") from exc
        if r.returncode == 0:
            value = r.stdout.strip().lower()
        elif r.returncode == 1:
            value = ""
        else:
            stderr = (r.stderr or "").strip()
            raise RuntimeError(
                f"读取 Git 配置失败，已中止导出: {name}\n{stderr}"
            )
        return value

    @staticmethod
    def _parse_check_attr_records(data: bytes) -> dict:
        """解析 ``check-attr -z --stdin`` 的 path/name/value 三元组。"""
        parts = data.split(b"\x00")
        records = {}
        for index in range(0, len(parts) - 2, 3):
            path = parts[index].decode("utf-8", errors="surrogateescape")
            name = parts[index + 1].decode("utf-8", errors="replace")
            try:
                value = parts[index + 2].decode(
                    "utf-8", errors="strict" if name == "filter" else "replace"
                )
            except UnicodeDecodeError as exc:
                raise RuntimeError(
                    f"无法准确解析 Git filter 名称，已中止导出: {path}"
                ) from exc
            # filter 名对应大小写敏感的 Git 配置子节，不能转成小写后查询
            # 另一个驱动，否则会把真正启用的转换误当成未配置。
            records.setdefault(path, {})[name] = value if name == "filter" else value.lower()
        return records

    def _get_checkout_attributes(self, version: str, file_path: str) -> dict:
        cache = getattr(self, "_attribute_cache", None)
        if cache is None:
            self._attribute_cache = {}
            cache = self._attribute_cache
        endpoint = self._resolve_version(version)
        cache_key = (endpoint, file_path)
        if cache_key in cache:
            return cache[cache_key]

        attrs = self._read_checkout_attributes(endpoint, file_path)
        cache[cache_key] = attrs
        return attrs

    def _read_checkout_attributes(self, endpoint: str, file_path: str) -> dict:
        return self._read_checkout_attributes_bulk(endpoint, [file_path])[file_path]

    def _read_checkout_attributes_bulk(self, endpoint: str, file_paths) -> dict:
        paths = sorted(set(file_paths))
        if not paths:
            return {}
        stdin_payload = b"\x00".join(
            self._repo_path(path).encode("utf-8", errors="surrogateescape") for path in paths
        ) + b"\x00"
        result = subprocess.run(
            [self._git, "check-attr", "-z", f"--source={endpoint}",
             "text", "eol", "filter", "working-tree-encoding", "ident", "crlf",
             "--stdin"],
            cwd=self._git_cwd(),
            input=stdin_payload,
            capture_output=True,
            timeout=self.COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"无法批量读取 Git 属性，已中止导出\n{stderr.strip()}"
            )
        records = self._parse_check_attr_records(result.stdout)
        records = {path: records[self._repo_path(path)] for path in paths
                   if self._repo_path(path) in records}
        missing = [path for path in paths if path not in records]
        if missing:
            preview = ", ".join(missing[:3])
            raise RuntimeError(
                f"Git 属性查询结果不完整，已中止导出: {preview}"
            )
        return {path: records[path] for path in paths}

    def _read_filter_checkout_config(self, driver: str) -> dict:
        """只读取检出方向的有效配置，不执行仓库指定的转换程序。"""
        config = {}
        for setting in ("smudge", "process", "required"):
            name = f"filter.{driver}.{setting}"
            args = [self._git, "config", "-z"]
            if setting == "required":
                args.append("--bool")
            args.extend(["--get", name])
            try:
                result = subprocess.run(
                    args, cwd=self._git_cwd(), capture_output=True,
                    timeout=self.COMMAND_TIMEOUT,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
                raise RuntimeError(f"无法读取 Git filter 配置 {name}: {exc}") from exc
            if result.returncode == 1:
                value = b""
            elif result.returncode == 0 and result.stdout.endswith(b"\x00"):
                # 保留命令的空格和大小写；空字符串才表示未配置程序。
                value = result.stdout[:-1]
            else:
                raise RuntimeError(
                    f"读取 Git filter 配置失败，已中止导出: {name}\n"
                    + result.stderr.decode("utf-8", errors="replace")
                )
            config[setting] = value == b"true" if setting == "required" else value
        return config

    def _filter_is_passthrough(self, driver: str) -> bool:
        cache = getattr(self, "_filter_config_cache", None)
        if cache is None:
            self._filter_config_cache = {}
            cache = self._filter_config_cache
        if driver not in cache:
            first = self._read_filter_checkout_config(driver)
            if first != self._read_filter_checkout_config(driver):
                raise RuntimeError("Git filter 配置在任务快照期间发生变化，已中止生成")
            cache[driver] = first
        config = cache[driver]
        # clean 只作用于入库方向；没有检出程序且不要求转换时，Git 保留原文。
        return not (config["smudge"] or config["process"] or config["required"])

    def _validate_checkout_attributes(self, file_path: str, attrs: dict):
        unsupported = []
        for name in ("filter", "working-tree-encoding", "ident", "crlf"):
            value = attrs.get(name, "unspecified")
            if name == "filter" and value in ("unspecified", "unset", "set"):
                # CLI 输出不能区分状态与同名字面值，交给隔离的 Git 判断。
                self._filter_is_passthrough(value)
                continue
            if value not in ("unspecified", "unset"):
                if name == "filter" and self._filter_is_passthrough(value):
                    continue
                unsupported.append(f"{name}={value}")
        if unsupported:
            raise RuntimeError(
                "Git 文件启用了当前无法可靠复现的检出属性，已中止导出: "
                f"{file_path}\n属性: {', '.join(unsupported)}"
            )

    def _snapshot_checkout_policy(self, endpoints):
        """一次性固定本次任务使用的 config 与端点有效属性。"""
        unique = sorted(set((str(version), path) for version, path in endpoints))
        if not unique or not hasattr(self, "_git"):
            return
        config_names = ("core.autocrlf", "core.eol")
        first_config = dict(getattr(self, "_config_cache", {}))
        if any(name not in first_config for name in config_names):
            self._snapshot_git_config()
            first_config = dict(self._config_cache)
        grouped = {}
        for version, path in unique:
            grouped.setdefault(self._resolve_version(version), []).append(path)

        def read_all():
            snapshot = {}
            for endpoint, paths in sorted(grouped.items()):
                for path, attrs in self._read_checkout_attributes_bulk(
                    endpoint, paths
                ).items():
                    snapshot[(endpoint, path)] = attrs
            return snapshot

        if getattr(self, "_checkout_snapshot", None) is None:
            self._checkout_snapshot = GitCheckoutSnapshot(self, first_config)
        self._checkout_snapshot.verify_source(self)
        first_attrs = read_all()
        drivers = sorted({
            attrs["filter"] for attrs in first_attrs.values()
            if "filter" in attrs
        })
        first_filters = {
            driver: self._read_filter_checkout_config(driver) for driver in drivers
        }
        second_config = {
            name: self._read_git_config_value(name) for name in config_names
        }
        second_attrs = read_all()
        second_filters = {
            driver: self._read_filter_checkout_config(driver) for driver in drivers
        }
        if (
            first_config != second_config or first_attrs != second_attrs
            or first_filters != second_filters
        ):
            raise RuntimeError(
                "Git 检出配置或属性在任务快照期间发生变化，已中止生成"
            )
        self._checkout_snapshot.verify_source(self)
        self._config_cache = first_config
        self._attribute_cache = first_attrs
        self._filter_config_cache = first_filters

    def _ensure_checkout_snapshot(self, endpoints):
        if getattr(self, "_checkout_snapshot", None) is None:
            self._snapshot_checkout_policy(endpoints)

    def _snapshot_git_config(self):
        if not hasattr(self, "_git"):
            return
        names = ("core.autocrlf", "core.eol")
        first = {name: self._read_git_config_value(name) for name in names}
        second = {name: self._read_git_config_value(name) for name in names}
        if first != second:
            raise RuntimeError("Git 检出配置在任务快照期间发生变化，已中止生成")
        self._config_cache = first

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
