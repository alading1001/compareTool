import os
import shutil
from typing import List

from path_safety import is_link_or_junction, safe_join
from .base import BaseVCS, ChangedFile, ChangeType
from .temp_storage import create_temp_dir, remove_temp_dir
from logger import warn


class FolderVCS(BaseVCS):
    """文件夹直接比对实现"""

    def __init__(self, old_dir: str, new_dir: str, snapshot: bool = True):
        source_old = os.path.realpath(os.path.abspath(old_dir))
        source_new = os.path.realpath(os.path.abspath(new_dir))
        try:
            common = os.path.commonpath([source_old, source_new])
        except ValueError:
            common = ""
        if os.path.normcase(common) in (
            os.path.normcase(source_old), os.path.normcase(source_new)
        ):
            raise ValueError("旧、新版本文件夹必须不同且不能互为祖先目录")

        super().__init__(new_dir)  # 报告仍显示用户选择的新版本目录
        self.source_old_dir = old_dir
        self.source_new_dir = new_dir
        self._owned_temp_dirs = []
        self.required_directory_deletions = []
        try:
            if snapshot:
                self.old_dir = self._snapshot_directory(old_dir, "comparetool_folder_old_")
                self.new_dir = self._snapshot_directory(new_dir, "comparetool_folder_new_")
            else:
                self.old_dir = old_dir
                self.new_dir = new_dir
        except BaseException:
            self.cleanup()
            raise

    def _walk_tree(self, root: str):
        """遍历目录，返回文件与目录的相对路径集合。"""
        files = set()
        directories = set()
        if not os.path.isdir(root):
            return files, directories
        if is_link_or_junction(root):
            raise RuntimeError(f"不允许将符号链接或联接点作为比对根目录: {root}")
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接或联接点，已拒绝读取: {full}")
                directories.add(os.path.relpath(full, root).replace("\\", "/"))
            for f in filenames:
                full = os.path.join(dirpath, f)
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接，已拒绝读取: {full}")
                rel = os.path.relpath(full, root).replace("\\", "/")
                files.add(rel)
        return files, directories

    def _snapshot_directory(self, source: str, prefix: str) -> str:
        files, directories = self._walk_tree(source)
        target = create_temp_dir(prefix=prefix)
        self._owned_temp_dirs.append(target)
        for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
            os.makedirs(self._resolve_file_path(target, directory), exist_ok=True)
        for rel_path in sorted(files):
            source_path = self._resolve_file_path(source, rel_path)
            target_path = self._resolve_file_path(target, rel_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(source_path, "rb") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
        return target

    def get_changed_files(self, old_version: str = "", new_version: str = "") -> List[ChangedFile]:
        """对比两个文件夹，返回差异文件列表"""
        old_files, old_dirs = self._walk_tree(self.old_dir)
        new_files, _new_dirs = self._walk_tree(self.new_dir)

        result = []

        for f in new_files - old_files:
            result.append(ChangedFile(path=f, change_type=ChangeType.ADDED))

        for f in old_files - new_files:
            result.append(ChangedFile(path=f, change_type=ChangeType.DELETED))

        for f in old_files & new_files:
            # 同一文件路径但内容不同
            if not self._same_file_content(
                os.path.join(self.old_dir, f), os.path.join(self.new_dir, f)
            ):
                result.append(ChangedFile(path=f, change_type=ChangeType.MODIFIED))

        new_endpoint_files = {
            item.path for item in result
            if item.change_type in (ChangeType.ADDED, ChangeType.RENAMED)
        }
        self.required_directory_deletions = sorted(
            directory for directory in old_dirs
            if any(
                new_path == directory or directory.startswith(new_path.rstrip("/") + "/")
                for new_path in new_endpoint_files
            )
        )

        return self._filter_files(result)

    @staticmethod
    def _same_file_content(old_path: str, new_path: str) -> bool:
        if os.path.getsize(old_path) != os.path.getsize(new_path):
            return False
        with open(old_path, "rb") as old_stream, open(new_path, "rb") as new_stream:
            while True:
                old_chunk = old_stream.read(1024 * 1024)
                new_chunk = new_stream.read(1024 * 1024)
                if old_chunk != new_chunk:
                    return False
                if not old_chunk:
                    return True

    def _resolve_version_dir(self, version: str) -> str:
        """根据版本标识解析实际目录路径"""
        if version in ("old", self.old_dir, self.source_old_dir):
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

    def cleanup(self):
        for path in self._owned_temp_dirs:
            if os.path.isdir(path):
                try:
                    remove_temp_dir(path)
                except OSError as exc:
                    warn(f"清理文件夹端点快照失败，已保留现场: {path}: {exc}")
        self._owned_temp_dirs = []

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
