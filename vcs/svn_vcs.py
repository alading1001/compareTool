import re
import shutil
import subprocess
import os
from typing import List
from urllib.parse import quote
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
        output = self._run(["diff", "--summarize", "--xml", f"-r{old_rev}:{new_rev}"])
        try:
            root = ElementTree.fromstring(output)
        except ElementTree.ParseError as exc:
            raise RuntimeError(f"无法解析 SVN 变更摘要: {exc}") from exc

        files = []
        for node in root.findall(".//path"):
            if node.get("kind") == "dir":
                continue
            path = node.text or ""
            if not path.strip():
                continue

            # svn diff 返回的是相对于项目目录的路径，先拼成绝对路径再算相对路径
            # 避免 Python 进程的 CWD 干扰 os.path.relpath 的结果
            abs_path = os.path.normpath(os.path.join(self.project_path, path))
            try:
                rel_path = os.path.relpath(abs_path, self.project_path)
            except ValueError:
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
            if change_type is None and item in ("none", "normal") and node.get("props") == "modified":
                change_type = ChangeType.MODIFIED
            if change_type is not None:
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
        return files

    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        files = self._parse_svn_diff_summarize(old_version, new_version)
        return self._filter_files(files)

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
        rev = str(version).lstrip("rR")
        relative = quote(file_path.replace("\\", "/").strip("/"), safe="/")
        return f"{self._repo_url.rstrip('/')}/{relative}@{rev}"

    def _get_properties(self, version: str, file_path: str) -> dict:
        cache = getattr(self, "_property_cache", None)
        if cache is None:
            self._property_cache = {}
            cache = self._property_cache
        cache_key = (str(version), file_path)
        if cache_key in cache:
            return cache[cache_key]
        rev = str(version).lstrip("rR")
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
