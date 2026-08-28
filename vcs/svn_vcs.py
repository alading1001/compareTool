import re
import shutil
import subprocess
import os
from typing import List
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

from .base import BaseVCS, ChangedFile, ChangeType
from logger import info, warn, error, cmd as log_cmd


SVN_NOT_FOUND_MESSAGE = (
    "未找到 SVN 命令行工具 svn.exe。\n"
    "请安装 SVN 命令行工具；如果使用 TortoiseSVN，请重新安装并勾选 command line client tools。"
)


def _decode_bytes(data: bytes) -> str:
    """自动检测编码：UTF-8 → GBK → 回退"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class SVNVCS(BaseVCS):
    """SVN版本控制实现"""

    def __init__(self, project_path: str, svn_path: str = ""):
        super().__init__(project_path)
        self._svn = svn_path or self._find_svn()
        self._version_pins = {}
        self._project_url_cache = {}

    @staticmethod
    def _find_svn() -> str:
        """自动探测 svn 可执行文件路径"""
        # 1. 先从当前进程 PATH 找
        found = shutil.which("svn")
        if found:
            info(f"自动探测 svn (PATH): {found}")
            return found
        # 2. Windows: 合并注册表中的用户/系统 PATH 后再找
        if os.name == "nt":
            try:
                import winreg
                extra_paths = []
                for root, key in [(winreg.HKEY_CURRENT_USER, "Environment"),
                                  (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")]:
                    try:
                        with winreg.OpenKey(root, key) as regkey:
                            extra_paths.append(winreg.QueryValueEx(regkey, "Path")[0])
                    except OSError:
                        pass
                merged = os.environ.get("PATH", "") + ";" + ";".join(extra_paths)
                for p in merged.split(";"):
                    p = p.strip().strip('"')
                    candidate = os.path.join(p, "svn.exe")
                    if os.path.isfile(candidate):
                        info(f"自动探测 svn (注册表PATH): {candidate}")
                        return candidate
            except Exception:
                pass
            # 3. Windows 常见安装位置
            for p in [
                r"C:\Program Files\TortoiseSVN\bin\svn.exe",
                r"C:\Program Files (x86)\TortoiseSVN\bin\svn.exe",
                r"C:\Program Files\VisualSVN\bin\svn.exe",
                r"C:\Program Files\SlikSvn\bin\svn.exe",
                r"C:\Program Files\CollabNet\Subversion Client\svn.exe",
            ]:
                if os.path.isfile(p):
                    info(f"自动探测 svn (常见位置): {p}")
                    return p
        # 4. 回退到 'svn'
        warn("未找到 svn，回退使用 'svn'")
        return "svn"

    @property
    def _repo_url(self) -> str:
        """仓库 URL，懒加载并缓存"""
        if not hasattr(self, '_cached_repo_url'):
            try:
                self._cached_repo_url = self._run(["info", "--non-interactive", "--show-item", "url"]).strip()
            except RuntimeError:
                self._cached_repo_url = ""
        return self._cached_repo_url

    @staticmethod
    def _parse_info_identity(output: str, context: str):
        try:
            root = ElementTree.fromstring(output)
        except ElementTree.ParseError as exc:
            raise RuntimeError(f"无法解析 SVN 仓库身份信息: {context}") from exc
        entry = root.find("entry")
        if entry is None:
            raise RuntimeError(f"SVN 仓库身份信息缺少 entry: {context}")
        url = (entry.findtext("url") or "").strip()
        repo_root = (entry.findtext("./repository/root") or "").strip()
        uuid = (entry.findtext("./repository/uuid") or "").strip()
        revision = (entry.get("revision") or "").strip()
        if not url or not repo_root or not uuid or not revision.isdigit():
            raise RuntimeError(f"SVN 仓库身份信息不完整: {context}")
        return url.rstrip("/"), repo_root.rstrip("/"), uuid, revision

    def _pin_source_identity(self):
        """一次性固定工作副本对应的 URL、UUID 和可用于寻址历史的 peg。"""
        if getattr(self, "_source_identity_pinned", False):
            return
        output = self._run([
            "info", "--xml", "--non-interactive", "-r", "HEAD", ".",
        ])
        url, repo_root, uuid, peg_revision = self._parse_info_identity(
            output, self.project_path
        )
        # 所有字段完整取得后再发布，避免半初始化状态被后续读取复用。
        self._pinned_project_url = url
        self._pinned_repo_root_url = repo_root
        self._pinned_repo_uuid = uuid
        self._pinned_peg_revision = peg_revision
        self._cached_repo_url = url
        self._project_url_cache = {peg_revision: url}
        self._source_identity_pinned = True

    def _project_url_at(self, version: str) -> str:
        """沿固定 peg 的节点历史解析某个 revision 的项目根 URL。"""
        rev = self._resolve_version(version)
        if not getattr(self, "_source_identity_pinned", False):
            # 兼容只直接调用内容读取 helper 的现有调用者；正式任务入口会先
            # 调用 _pin_source_identity，因而不会再读取可变工作副本状态。
            return self._repo_url.rstrip("/")
        cache = getattr(self, "_project_url_cache", None)
        if cache is None:
            self._project_url_cache = {}
            cache = self._project_url_cache
        if rev in cache:
            return cache[rev]
        target = f"{self._pinned_project_url}@{self._pinned_peg_revision}"
        output = self._run([
            "info", "--xml", "--non-interactive", "-r", rev, target,
        ])
        url, repo_root, uuid, _ = self._parse_info_identity(
            output, f"{target} -r {rev}"
        )
        if uuid != self._pinned_repo_uuid or repo_root != self._pinned_repo_root_url:
            raise RuntimeError(
                "SVN 仓库身份在任务期间发生变化，已中止生成："
                f"{self._pinned_repo_uuid} -> {uuid}"
            )
        cache[rev] = url
        return url

    def _run(self, args: list) -> str:
        full_cmd = [self._svn] + args
        info(f"SVN cmd (text): {' '.join(full_cmd)}")
        try:
            result = subprocess.run(
                full_cmd,
                cwd=self.project_path,
                capture_output=True
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            warn(f"SVN cmd FAIL: {' '.join(full_cmd)} | rc={result.returncode} | {stderr[:200]}")
            raise RuntimeError(f"SVN命令失败: {' '.join(args)}\n{stderr}")
        info(f"SVN cmd OK: rc=0")
        return _decode_bytes(result.stdout)

    def _run_bytes(self, args: list) -> bytes:
        """执行SVN命令并返回原始字节（用于获取文件内容）"""
        full_cmd = [self._svn] + args
        info(f"SVN cmd (bytes): {' '.join(full_cmd)}")
        try:
            result = subprocess.run(
                full_cmd,
                cwd=self.project_path,
                capture_output=True,
                timeout=30
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            warn(f"SVN bytes FAIL: {' '.join(full_cmd)} | rc={result.returncode} | {stderr[:200]}")
            raise RuntimeError(f"SVN命令失败: {' '.join(args)}\n{stderr}")
        info(f"SVN bytes OK: rc=0, len={len(result.stdout)}")
        return result.stdout

    def _parse_svn_diff_summarize(self, old_rev: str, new_rev: str) -> List[ChangedFile]:
        """使用 XML 摘要获取变更文件，可靠区分文件、目录和替换节点。"""
        target = f"{self._pinned_project_url}@{self._pinned_peg_revision}"
        output = self._run([
            "diff", "--summarize", "--xml", "--notice-ancestry",
            f"-r{old_rev}:{new_rev}", target,
        ])
        try:
            root = ElementTree.fromstring(output)
        except ElementTree.ParseError as exc:
            raise RuntimeError(f"无法解析 SVN 变更摘要: {exc}") from exc

        files = []
        deleted_directories = set()
        new_file_paths = set()
        for node in root.findall(".//path"):
            if node.get("kind") == "dir":
                path = self._summary_relative_path(
                    node.text or "", old_rev, new_rev
                )
                item = node.get("item", "")
                if path.strip():
                    rel_dir = path
                    old_props = (
                        self._get_properties(old_rev, rel_dir)
                        if item in ("deleted", "replaced", "modified") else {}
                    )
                    new_props = (
                        self._get_properties(new_rev, rel_dir)
                        if item in ("added", "replaced", "modified") else {}
                    )
                    if "svn:externals" in old_props or "svn:externals" in new_props:
                        raise RuntimeError(
                            "SVN 目录启用了 svn:externals，文件级交付无法保真，"
                            f"已中止生成: {rel_dir}"
                        )
                if node.get("props") == "modified":
                    raise RuntimeError(
                        "SVN 目录属性发生变化，当前文件级交付无法保真，已中止生成: "
                        + path
                    )
                if item in ("deleted", "replaced") and path.strip():
                    rel_dir = path
                    if item == "replaced" and self._get_node_kind(old_rev, rel_dir) == "file":
                        metadata = self._compare_endpoint_metadata(
                            old_rev, rel_dir, new_rev, None
                        )
                        files.append(ChangedFile(
                            path=rel_dir,
                            change_type=ChangeType.DELETED,
                            **metadata,
                        ))
                    else:
                        deleted_directories.add(rel_dir)
                continue
            path = self._summary_relative_path(node.text or "", old_rev, new_rev)
            if not path.strip():
                continue
            rel_path = path

            change_map = {
                "added": ChangeType.ADDED,
                "modified": ChangeType.MODIFIED,
                "deleted": ChangeType.DELETED,
                # SVN 的 replaced 是同一路径节点替换，不是重命名。
                "replaced": ChangeType.MODIFIED,
            }
            item = node.get("item", "")
            change_type = change_map.get(item)
            if item == "replaced" and self._get_node_kind(old_rev, rel_path) == "dir":
                deleted_directories.add(rel_path)
                change_type = ChangeType.ADDED
            if change_type is None and item in ("none", "normal") and node.get("props") == "modified":
                change_type = ChangeType.MODIFIED
            if change_type is not None:
                if change_type in (ChangeType.ADDED, ChangeType.RENAMED):
                    new_file_paths.add(rel_path)
                metadata = self._compare_endpoint_metadata(
                    old_rev,
                    None if change_type == ChangeType.ADDED else rel_path,
                    new_rev,
                    None if change_type == ChangeType.DELETED else rel_path,
                )
                files.append(ChangedFile(
                    path=rel_path,
                    change_type=change_type,
                    **metadata,
                ))
            elif item not in ("none", "normal"):
                raise RuntimeError(f"暂不支持的 SVN 变更类型 {item}: {path}")
        self.required_directory_deletions = sorted(
            directory for directory in deleted_directories
            if any(
                new_path.casefold() == directory.casefold()
                or directory.casefold().startswith(
                    new_path.rstrip("/").casefold() + "/"
                )
                for new_path in new_file_paths
            )
        )
        return files

    def _summary_relative_path(self, raw_path: str, old_rev: str, new_rev: str) -> str:
        """把 svn diff XML 的 URL/绝对路径/相对路径统一为正斜杠相对路径。"""
        value = (raw_path or "").strip()
        if not value:
            return ""
        if urlsplit(value).scheme:
            for base in (self._project_url_at(old_rev), self._project_url_at(new_rev)):
                prefix = base.rstrip("/")
                if value.casefold() == prefix.casefold():
                    return ""
                marker = prefix + "/"
                if value[:len(marker)].casefold() == marker.casefold():
                    return unquote(value[len(marker):]).replace("\\", "/").strip("/")
            raise RuntimeError(f"SVN 变更摘要包含项目根之外的 URL，已中止生成: {value}")

        normalized = value.replace("\\", "/")
        if os.path.isabs(value):
            try:
                normalized = os.path.relpath(
                    os.path.normpath(value), os.path.abspath(self.project_path)
                ).replace("\\", "/")
            except ValueError as exc:
                raise RuntimeError(
                    f"SVN 变更摘要路径无法映射到项目目录: {value}"
                ) from exc
        normalized = os.path.normpath(normalized).replace("\\", "/")
        if normalized == ".":
            return ""
        if normalized == ".." or normalized.startswith("../") or os.path.isabs(normalized):
            raise RuntimeError(f"SVN 变更摘要路径越出项目目录，已中止生成: {value}")
        return normalized.strip("/")

    def _get_node_kind(self, version: str, path: str) -> str:
        cache = getattr(self, "_node_kind_cache", None)
        if cache is None:
            self._node_kind_cache = {}
            cache = self._node_kind_cache
        key = (self._resolve_version(version), path)
        if key in cache:
            return cache[key]
        try:
            result = subprocess.run(
                [
                    self._svn, "info", "--non-interactive", "--show-item", "kind",
                    "-r", key[0], self._file_url(key[0], path),
                ],
                cwd=self.project_path,
                capture_output=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"无法确认 SVN 替换前节点类型，已中止生成: {path}@{version}\n{exc}"
            ) from exc
        kind = _decode_bytes(result.stdout).strip().lower() if result.returncode == 0 else ""
        if kind not in ("file", "dir"):
            raise RuntimeError(
                f"无法确认 SVN 替换前节点类型，已中止生成: {path}@{version}\n"
                + _decode_bytes(result.stderr or result.stdout)
            )
        cache[key] = kind
        return kind

    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        self._pin_source_identity()
        old_endpoint = self._pin_version(old_version)
        new_endpoint = self._pin_version(new_version)
        files = self._parse_svn_diff_summarize(old_endpoint, new_endpoint)
        return self._filter_files(files)

    def _pin_version(self, version: str) -> str:
        """把 HEAD 等可变标识固定为本次任务开始时的数字 revision。"""
        self._pin_source_identity()
        key = str(version)
        pins = getattr(self, "_version_pins", None)
        if pins is None:
            self._version_pins = {}
            pins = self._version_pins
        if key in pins:
            return pins[key]
        raw = key.lstrip("rR")
        if raw.isdigit():
            pins[key] = raw
            return raw
        target = f"{self._pinned_project_url}@{self._pinned_peg_revision}"
        output = self._run([
            "info", "--xml", "--non-interactive", "-r", raw, target,
        ])
        _, repo_root, uuid, resolved = self._parse_info_identity(
            output, f"{target} -r {raw}"
        )
        if uuid != self._pinned_repo_uuid or repo_root != self._pinned_repo_root_url:
            raise RuntimeError("SVN 版本端点不属于任务开始时固定的仓库，已中止生成")
        if not resolved.isdigit():
            raise RuntimeError(f"SVN 版本端点解析结果无效: {version} -> {resolved}")
        pins[key] = resolved
        return resolved

    def _resolve_version(self, version: str) -> str:
        return getattr(self, "_version_pins", {}).get(
            str(version), str(version).lstrip("rR")
        )

    def get_file_content(self, version: str, file_path: str) -> str:
        try:
            data = self._run_bytes(["cat", self._file_url(version, file_path)])
            return _decode_bytes(data)
        except RuntimeError:
            return ""

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        data = self.get_file_content_raw_bytes(version, file_path)
        if data is None or not self._is_text_bytes(data):
            return data

        style = self._get_eol_style(version, file_path).strip().lower()
        if style == "crlf" or (style == "native" and os.linesep == "\r\n"):
            data = self._apply_crlf(self._normalize_lf(data))
        elif style == "cr" or (style == "native" and os.linesep == "\r"):
            data = self._normalize_lf(data).replace(b"\n", b"\r")
        elif style == "lf":
            data = self._normalize_lf(data)
        return data

    @staticmethod
    def _normalize_lf(data: bytes) -> bytes:
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    def get_file_content_raw_bytes(self, version: str, file_path: str) -> bytes:
        """读取仓库中的原始字节，不应用工作副本换行符转换。"""
        try:
            return self._run_bytes(["cat", self._file_url(version, file_path)])
        except RuntimeError:
            return None

    def _get_eol_style(self, version: str, file_path: str) -> str:
        """按所选 revision 从仓库读取 svn:eol-style（删除文件也能正确读取）。"""
        cache = getattr(self, '_eol_cache', None)
        if cache is None:
            self._eol_cache = {}
        cache_key = (version, file_path)
        if cache_key in self._eol_cache:
            return self._eol_cache[cache_key]
        style = self._get_properties(version, file_path).get("svn:eol-style", "")
        self._eol_cache[cache_key] = style
        return style

    def _file_url(self, version: str, file_path: str) -> str:
        rev = self._resolve_version(version)
        relative = quote(file_path.replace("\\", "/").strip("/"), safe="/")
        root_url = self._project_url_at(rev)
        suffix = f"/{relative}" if relative else ""
        return f"{root_url.rstrip('/')}{suffix}@{rev}"

    def _get_properties(self, version: str, file_path: str) -> dict:
        cache = getattr(self, "_property_cache", None)
        if cache is None:
            self._property_cache = {}
            cache = self._property_cache
        resolved_version = self._resolve_version(version)
        cache_key = (resolved_version, file_path)
        if cache_key in cache:
            return cache[cache_key]
        rev = resolved_version
        try:
            result = subprocess.run(
                [
                    self._svn,
                    "proplist",
                    "--xml",
                    "-v",
                    "--non-interactive",
                    "-r",
                    rev,
                    self._file_url(version, file_path),
                ],
                cwd=self.project_path,
                capture_output=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"无法读取 SVN 文件属性，已中止导出: {file_path}@{version}\n{exc}"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"无法读取 SVN 文件属性，已中止导出: {file_path}@{version}\n"
                + _decode_bytes(result.stderr or result.stdout)
            )
        try:
            root = ElementTree.fromstring(_decode_bytes(result.stdout))
        except ElementTree.ParseError as exc:
            raise RuntimeError(
                f"无法解析 SVN 文件属性，已中止导出: {file_path}@{version}"
            ) from exc
        properties = {}
        for node in root.findall(".//property"):
            name = node.get("name", "")
            if not name:
                continue
            value = node.text or ""
            if node.get("encoding"):
                value = f"{node.get('encoding')}:{value}"
            properties[name] = value
        cache[cache_key] = properties
        return properties

    def _compare_endpoint_metadata(
            self, old_version, old_path, new_version, new_path) -> dict:
        old_props = self._get_properties(old_version, old_path) if old_path else {}
        new_props = self._get_properties(new_version, new_path) if new_path else {}
        if "svn:special" in old_props or "svn:special" in new_props:
            special_path = new_path or old_path
            raise RuntimeError(
                "SVN 端点是 svn:special（符号链接等特殊节点），普通文件导出无法保真，"
                f"已中止生成: {special_path}"
            )
        if "svn:keywords" in old_props or "svn:keywords" in new_props:
            raise RuntimeError(
                "SVN 文件启用了 svn:keywords，svn cat 不能可靠复现工作副本展开字节，"
                f"已中止生成: {new_path or old_path}"
            )
        changed = {
            name for name in set(old_props) | set(new_props)
            if old_props.get(name) != new_props.get(name)
        }
        supported = {"svn:eol-style", "svn:executable"}
        unsupported = sorted(changed - supported)
        if unsupported:
            raise RuntimeError(
                "SVN 文件属性发生变化，但普通文件交付无法保真，已中止生成: "
                f"{new_path or old_path}\n属性: {', '.join(unsupported)}"
            )
        details = []
        if "svn:eol-style" in changed:
            details.append(
                "SVN 换行属性："
                f"{old_props.get('svn:eol-style', '未设置')} → "
                f"{new_props.get('svn:eol-style', '未设置')}"
            )
        if "svn:executable" in changed:
            details.append(
                "SVN 可执行属性："
                f"{'已设置' if 'svn:executable' in old_props else '未设置'} → "
                f"{'已设置' if 'svn:executable' in new_props else '未设置'}"
            )
        return {
            "metadata_changes": details,
            "old_executable": (
                "svn:executable" in old_props if old_path else None
            ),
            "new_executable": (
                "svn:executable" in new_props if new_path else None
            ),
        }

    def get_file_content_working(self, file_path: str) -> str:
        full_path = os.path.join(self.project_path, file_path)
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            return ""
        with open(full_path, "rb") as f:
            return _decode_bytes(f.read())

    def get_versions(self) -> List[str]:
        """获取SVN的最近100个revision（从HEAD:1查询，确保拿到最新）"""
        try:
            output = self._run(["log", "-r", "HEAD:1", "-l", "100"])
            revisions = []
            # SVN log 用分隔线分开每个 entry
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

                # 提取 commit message：跳过 "Changed paths:" 及其后续缩进行
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
                if msg:
                    suffix = "..." if len(msg) > 60 else ""
                    revisions.append(f"{rev} {msg[:60]}{suffix}")
                else:
                    revisions.append(rev)
            return revisions
        except RuntimeError as exc:
            if SVN_NOT_FOUND_MESSAGE in str(exc):
                raise
            return []

    def check_version_exists(self, version: str) -> bool:
        rev = version.lstrip("r")
        try:
            self._run(["log", f"-r{rev}", "--limit", "1"])
            return True
        except RuntimeError as exc:
            if SVN_NOT_FOUND_MESSAGE in str(exc):
                raise
            return False
