import os
import shutil
from diff_engine import DiffResult
from vcs.base import ChangeType


class FileExporter:
    """将变更文件导出到指定目录"""

    def __init__(self, diff_result: DiffResult, vcs):
        self.diff_result = diff_result
        self.vcs = vcs

    def export(self, old_dir: str, new_dir: str):
        """导出新旧版本的变更文件（先清空目标目录再导出）"""
        for d in (old_dir, new_dir):
            if os.path.isdir(d):
                shutil.rmtree(d)
            os.makedirs(d, exist_ok=True)

        for file_diff in self.diff_result.files:
            if file_diff.change_type == ChangeType.DELETED:
                # 仅导出旧版本
                self._write_file(old_dir, file_diff.file_path, file_diff.old_content)
            elif file_diff.change_type == ChangeType.ADDED:
                # 仅导出新版本
                self._write_file(new_dir, file_diff.file_path, file_diff.new_content)
            else:
                # 修改或重命名，两个版本都导出
                self._write_file(old_dir, file_diff.file_path, file_diff.old_content)
                self._write_file(new_dir, file_diff.file_path, file_diff.new_content)

    def _write_file(self, base_dir: str, rel_path: str, content: str):
        file_path = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
