import os
import filecmp
from typing import List

from path_safety import is_link_or_junction, safe_join
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
        if is_link_or_junction(root):
            raise RuntimeError(f"不允许将符号链接或联接点作为比对根目录: {root}")
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接或联接点，已拒绝读取: {full}")
            for f in filenames:
                full = os.path.join(dirpath, f)
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接，已拒绝读取: {full}")
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

    def _resolve_version_dir(self, version: str) -> str:
        """根据版本标识解析实际目录路径"""
        if version in ("old", self.old_dir):
            return self.old_dir
        return self.new_dir

    def get_file_content(self, version: str, file_path: str) -> str:
        folder = self._resolve_version_dir(version)
        full_path = self._resolve_file_path(folder, file_path)
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

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        folder = self._resolve_version_dir(version)
        full_path = self._resolve_file_path(folder, file_path)
        if not os.path.isfile(full_path):
            return None
        with open(full_path, "rb") as f:
            return f.read()

    @staticmethod
    def _resolve_file_path(folder: str, file_path: str) -> str:
        try:
            full_path = safe_join(folder, file_path, label="比对文件路径")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if is_link_or_junction(full_path):
            raise RuntimeError(f"比对文件是符号链接或联接点，已拒绝读取: {full_path}")
        root_real = os.path.realpath(os.path.abspath(folder))
        target_real = os.path.realpath(full_path)
        try:
            inside = os.path.commonpath([root_real, target_real]) == root_real
        except ValueError:
            inside = False
        if not inside:
            raise RuntimeError(f"比对文件解析后越界，已拒绝读取: {file_path}")
        return full_path

    def get_file_content_working(self, file_path: str) -> str:
        return self.get_file_content("new", file_path)

    def get_versions(self) -> List[str]:
        return []

    def check_version_exists(self, version: str) -> bool:
        return True
