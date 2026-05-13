import os
import shutil
from diff_engine import DiffResult
from vcs.base import ChangeType


class FileExporter:
    """将变更文件导出到指定目录"""

    def __init__(self, diff_result: DiffResult, vcs):
        self.diff_result = diff_result
        self.vcs = vcs

    def export(self, old_dir: str, new_dir: str, project_name: str = ""):
        """导出新旧版本的变更文件（先清空目标目录再导出）"""
        if project_name:
            old_dir = os.path.join(old_dir, project_name)
            new_dir = os.path.join(new_dir, project_name)

        for d in (old_dir, new_dir):
            if os.path.isdir(d):
                shutil.rmtree(d)
            os.makedirs(d, exist_ok=True)

        old_ver = self.diff_result.old_version
        new_ver = self.diff_result.new_version

        for file_diff in self.diff_result.files:
            if file_diff.change_type == ChangeType.DELETED:
                self._write_file(old_dir, file_diff.file_path, old_ver, file_diff.old_content)
            elif file_diff.change_type == ChangeType.ADDED:
                self._write_file(new_dir, file_diff.file_path, new_ver, file_diff.new_content)
            else:
                self._write_file(old_dir, file_diff.file_path, old_ver, file_diff.old_content)
                self._write_file(new_dir, file_diff.file_path, new_ver, file_diff.new_content)

    def _write_file(self, base_dir: str, rel_path: str, version: str, text_content: str):
        file_path = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # 优先读原始字节以保留原始编码，二进制和文本文件统一处理
        raw = self.vcs.get_file_content_bytes(version, rel_path)
        if raw is not None:
            with open(file_path, "wb") as f:
                f.write(raw)
        elif text_content:
            # 回退：无法获取原始字节时（如文件不存在于该版本），写文本内容
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text_content)
