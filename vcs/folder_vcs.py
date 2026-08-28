import os
import hashlib
import shutil
import stat
import threading
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
        self._snapshot_requested = snapshot
        self._snapshot_lock = threading.Lock()
        self._snapshotted = not snapshot
        self.required_directory_deletions = []
        self.old_dir = old_dir
        self.new_dir = new_dir

    def _ensure_snapshot(self):
        if self._snapshotted or not self._snapshot_requested:
            return
        with self._snapshot_lock:
            if self._snapshotted:
                return
            try:
                old_snapshot = self._snapshot_directory(
                    self.source_old_dir, "comparetool_folder_old_"
                )
                new_snapshot = self._snapshot_directory(
                    self.source_new_dir, "comparetool_folder_new_"
                )
            except BaseException:
                self.cleanup()
                self.old_dir = self.source_old_dir
                self.new_dir = self.source_new_dir
                raise
            self.old_dir = old_snapshot
            self.new_dir = new_snapshot
            self._snapshotted = True

    def _should_prune_directory(self, relative_path: str) -> bool:
        """仅当规则明确覆盖任意深度后代时才剪枝，避免误伤 foo/*。"""
        if not self.exclude_patterns:
            return False
        relative_path = relative_path.rstrip("/")
        direct_probe = f"{relative_path}/__comparetool_probe__"
        nested_probe = (
            f"{relative_path}/__comparetool_probe_dir__/__comparetool_probe__"
        )
        return self._is_excluded(direct_probe) and self._is_excluded(nested_probe)

    def _walk_tree(self, root: str, apply_excludes: bool = False):
        """遍历目录，返回文件与目录的相对路径集合。"""
        files = set()
        directories = set()
        if not os.path.isdir(root):
            raise RuntimeError(f"比对源目录不存在或不是目录: {root}")
        if is_link_or_junction(root):
            raise RuntimeError(f"不允许将符号链接或联接点作为比对根目录: {root}")

        def raise_walk_error(error):
            raise RuntimeError(f"遍历比对源目录失败: {root}: {error}") from error

        for dirpath, dirnames, filenames in os.walk(root, onerror=raise_walk_error):
            for name in list(dirnames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if apply_excludes and self._should_prune_directory(rel):
                    # 保留目录拓扑以正确识别“目录被文件替换”，但不进入或复制其内容。
                    directories.add(rel)
                    dirnames.remove(name)
                    continue
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接或联接点，已拒绝读取: {full}")
                directories.add(rel)
            for f in filenames:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if apply_excludes and self._is_excluded(rel):
                    continue
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接，已拒绝读取: {full}")
                files.add(rel)
        return files, directories

    @staticmethod
    def _signature_from_stat(metadata):
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    @staticmethod
    def _stream_state_from_stat(metadata):
        # Windows 上 path stat 与 handle fstat 的设备/文件编号表示可能不同；
        # 文件身份由复制前后的 path stat 校验，句柄只校验复制中的大小和 mtime。
        return metadata.st_size, metadata.st_mtime_ns

    def _file_signature(self, path: str):
        if is_link_or_junction(path):
            raise RuntimeError(f"比对目录包含符号链接或联接点，已拒绝读取: {path}")
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"读取比对源文件元数据失败: {path}: {exc}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"比对源包含非普通文件，已拒绝读取: {path}")
        return self._signature_from_stat(metadata)

    def _directory_identity(self, path: str):
        if not os.path.isdir(path):
            raise RuntimeError(f"比对源目录不存在或不是目录: {path}")
        if is_link_or_junction(path):
            raise RuntimeError(f"比对目录包含符号链接或联接点，已拒绝读取: {path}")
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"读取比对源目录元数据失败: {path}: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"比对源目录在快照期间被替换: {path}")
        return metadata.st_dev, metadata.st_ino

    def _snapshot_directory(self, source: str, prefix: str) -> str:
        root_identity = self._directory_identity(source)
        files, directories = self._walk_tree(source, apply_excludes=True)
        initial_directory_identities = {
            relative_path: self._directory_identity(
                self._resolve_file_path(source, relative_path)
            )
            for relative_path in directories
        }
        initial_file_signatures = {
            relative_path: self._file_signature(
                self._resolve_file_path(source, relative_path)
            )
            for relative_path in files
        }
        target = create_temp_dir(prefix=prefix)
        self._owned_temp_dirs.append(target)
        for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
            os.makedirs(self._resolve_file_path(target, directory), exist_ok=True)
        for rel_path in sorted(files):
            source_path = self._resolve_file_path(source, rel_path)
            target_path = self._resolve_file_path(target, rel_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            expected_signature = initial_file_signatures[rel_path]
            try:
                with open(source_path, "rb") as src, open(target_path, "wb") as dst:
                    expected_stream_state = (
                        expected_signature[2], expected_signature[3]
                    )
                    opened_stream_state = self._stream_state_from_stat(
                        os.fstat(src.fileno())
                    )
                    if opened_stream_state != expected_stream_state:
                        raise RuntimeError(
                            f"比对源文件在快照复制前发生变化: {source_path}"
                        )
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    copied_size = dst.tell()
                    closed_stream_state = self._stream_state_from_stat(
                        os.fstat(src.fileno())
                    )
            except OSError as exc:
                raise RuntimeError(f"复制比对源文件失败: {source_path}: {exc}") from exc
            if (
                copied_size != expected_signature[2]
                or closed_stream_state != expected_stream_state
                or self._file_signature(source_path) != expected_signature
            ):
                raise RuntimeError(
                    f"比对源文件在快照复制期间发生变化: {source_path}"
                )

        final_files, final_directories = self._walk_tree(
            source, apply_excludes=True
        )
        if final_files != files or final_directories != directories:
            raise RuntimeError(f"比对源目录路径集合在快照期间发生变化: {source}")
        if self._directory_identity(source) != root_identity:
            raise RuntimeError(f"比对源目录在快照期间被替换: {source}")
        for relative_path, expected_identity in initial_directory_identities.items():
            current_path = self._resolve_file_path(source, relative_path)
            if self._directory_identity(current_path) != expected_identity:
                raise RuntimeError(f"比对源目录在快照期间被替换: {current_path}")
        for relative_path, expected_signature in initial_file_signatures.items():
            current_path = self._resolve_file_path(source, relative_path)
            if self._file_signature(current_path) != expected_signature:
                raise RuntimeError(
                    f"比对源文件在快照期间发生变化: {current_path}"
                )
        return target

    def get_changed_files(self, old_version: str = "", new_version: str = "") -> List[ChangedFile]:
        """对比两个文件夹，返回差异文件列表"""
        self._ensure_snapshot()
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
                new_path.casefold() == directory.casefold()
                or directory.casefold().startswith(
                    new_path.rstrip("/").casefold() + "/"
                )
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
        self._ensure_snapshot()
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

    def get_file_size(self, version: str, file_path: str):
        folder = self._resolve_version_dir(version)
        full_path = self._resolve_file_path(folder, file_path)
        if not os.path.isfile(full_path):
            return None
        return os.path.getsize(full_path)

    def get_file_signature(self, version: str, file_path: str):
        folder = self._resolve_version_dir(version)
        full_path = self._resolve_file_path(folder, file_path)
        if not os.path.isfile(full_path):
            return None
        digest = hashlib.sha256()
        size = 0
        with open(full_path, "rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    def export_file_to_path(self, version: str, file_path: str, target_path: str):
        folder = self._resolve_version_dir(version)
        full_path = self._resolve_file_path(folder, file_path)
        if not os.path.isfile(full_path):
            raise RuntimeError(f"无法读取版本 {version} 中的文件: {file_path}")
        with open(full_path, "rb") as source, open(target_path, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)

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
