import re
import subprocess
import os
from typing import List

from .base import BaseVCS, ChangedFile, ChangeType
from logger import info, warn, error, cmd as log_cmd


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
        full_cmd = ["svn"] + args
        info(f"SVN cmd (text): {' '.join(full_cmd)}")
        result = subprocess.run(
            full_cmd,
            cwd=self.project_path,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            warn(f"SVN cmd FAIL: rc={result.returncode} stderr={result.stderr[:200]}")
            raise RuntimeError(f"SVN命令失败: {' '.join(args)}\n{result.stderr}")
        info(f"SVN cmd OK: rc=0")
        return result.stdout

    def _run_bytes(self, args: list) -> bytes:
        """执行SVN命令并返回原始字节（用于获取文件内容）"""
        full_cmd = ["svn"] + args
        info(f"SVN cmd (bytes): {' '.join(full_cmd)}")
        result = subprocess.run(
            full_cmd,
            cwd=self.project_path,
            capture_output=True,
            timeout=30
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            warn(f"SVN bytes FAIL: rc={result.returncode} stderr={stderr[:200]}")
            raise RuntimeError(f"SVN命令失败: {' '.join(args)}\n{stderr}")
        info(f"SVN bytes OK: rc=0, len={len(result.stdout)}")
        return result.stdout

    def _parse_svn_diff_summarize(self, old_rev: str, new_rev: str) -> List[ChangedFile]:
        """使用 svn diff --summarize 获取变更文件列表"""
        output = self._run(["diff", "--summarize", f"-r{old_rev}:{new_rev}"])
        files = []
        for line in output.strip().split("\n"):
            if not line:
                continue
            # 格式: "M       path/to/file" 或 "A       path/to/file"
            code = line[0].strip()
            path = line[1:].strip()
            # svn diff 返回的是相对于项目目录的路径，先拼成绝对路径再算相对路径
            # 避免 Python 进程的 CWD 干扰 os.path.relpath 的结果
            abs_path = os.path.normpath(os.path.join(self.project_path, path))
            try:
                rel_path = os.path.relpath(abs_path, self.project_path)
            except ValueError:
                rel_path = path

            change_map = {
                "A": ChangeType.ADDED,
                "M": ChangeType.MODIFIED,
                "D": ChangeType.DELETED,
                "R": ChangeType.RENAMED,
            }
            if code in change_map:
                files.append(ChangedFile(path=rel_path, change_type=change_map[code]))
        return files

    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        files = self._parse_svn_diff_summarize(old_version, new_version)
        return self._filter_files(files)

    def get_file_content(self, version: str, file_path: str) -> str:
        try:
            rev = version.lstrip("r")
            url = f"{self._repo_url}/{file_path.replace(chr(92), '/')}@{rev}"
            data = self._run_bytes(["cat", url])
            return _decode_bytes(data)
        except RuntimeError:
            return ""

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        try:
            rev = version.lstrip("r")
            url = f"{self._repo_url}/{file_path.replace(chr(92), '/')}@{rev}"
            return self._run_bytes(["cat", url])
        except RuntimeError:
            return b""

    def get_file_content_working(self, file_path: str) -> str:
        full_path = os.path.join(self.project_path, file_path)
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            return ""
        with open(full_path, "rb") as f:
            return _decode_bytes(f.read())

    def get_versions(self) -> List[str]:
        """获取SVN的最近50个revision（从HEAD:1查询，确保拿到最新）"""
        try:
            output = self._run(["log", "-r", "HEAD:1", "-l", "50"])
            revisions = []
            for line in output.split("\n"):
                m = re.match(r'^r(\d+) \|', line.strip())
                if m:
                    revisions.append(f"r{m.group(1)}")
            return revisions
        except RuntimeError:
            return []

    def check_version_exists(self, version: str) -> bool:
        rev = version.lstrip("r")
        try:
            self._run(["log", f"-r{rev}", "--limit", "1"])
            return True
        except RuntimeError:
            return False
