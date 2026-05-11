import os
import filecmp
from typing import List

from .base import BaseVCS, ChangedFile, ChangeType


class FolderVCS(BaseVCS):
    """文件夹直接比对实现"""

    def __init__(self, old_dir: str, new_dir: str):
        super().__init__(new_dir)  # project_path 设为 new_dir，供 DiffEngine 用
        self.old_dir = old_dir
        self.new_dir = new_dir

    def _walk_files(self, root: str) -> set:
        """遍历目录，返回所有文件相对路径集合"""
        result = set()
        if not os.path.isdir(root):
            return result
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root).replace("\\", "/")
                result.add(rel)
        return result

    def get_changed_files(self, old_version: str = "", new_version: str = "") -> List[ChangedFile]:
        """对比两个文件夹，返回差异文件列表"""
        old_files = self._walk_files(self.old_dir)
        new_files = self._walk_files(self.new_dir)

        result = []

        for f in new_files - old_files:
            result.append(ChangedFile(path=f, change_type=ChangeType.ADDED))

        for f in old_files - new_files:
            result.append(ChangedFile(path=f, change_type=ChangeType.DELETED))

        for f in old_files & new_files:
            # 同一文件路径但内容不同
            if not filecmp.cmp(
                os.path.join(self.old_dir, f),
                os.path.join(self.new_dir, f),
                shallow=False
            ):
                result.append(ChangedFile(path=f, change_type=ChangeType.MODIFIED))

        return self._filter_files(result)

    def get_file_content(self, version: str, file_path: str) -> str:
        folder = self.old_dir if version == "old" else self.new_dir
        full_path = os.path.join(folder, file_path)
        if not os.path.isfile(full_path):
            return ""
        with open(full_path, "rb") as f:
            data = f.read()
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def get_file_content_working(self, file_path: str) -> str:
        return self.get_file_content("new", file_path)

    def get_versions(self) -> List[str]:
        return []

    def check_version_exists(self, version: str) -> bool:
        return True
