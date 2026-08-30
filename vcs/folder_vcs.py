import os
import hashlib
import shutil
import stat
import threading
from typing import List

from path_safety import (
    is_link_or_junction,
    open_regular_file_no_links,
    regular_file_handle_identity,
    regular_file_path_identity,
    safe_join,
    windows_directories_replaced_by_files,
)
from .base import BaseVCS, ChangedFile, ChangeType
from .temp_storage import create_temp_dir, remove_temp_dir
from logger import warn


class FolderVCS(BaseVCS):
    """文件夹直接比对实现"""

    # 默认不因规模预测拒绝本来可以完成的文件夹任务；实际可用磁盘空间
    # 仍在复制前校验。数值仅作为显式策略/测试注入点。
    MAX_SNAPSHOT_FILES = None
    MAX_SNAPSHOT_ENTRIES = None
    MAX_SNAPSHOT_TOTAL_BYTES = None
    MIN_SNAPSHOT_FREE_BYTES = 0

    def __init__(self, old_dir: str, new_dir: str, snapshot: bool = True):
        source_old = os.path.realpath(os.path.abspath(old_dir))
        source_new = os.path.realpath(os.path.abspath(new_dir))
        try:
            # Windows 目录可以单独启用大小写敏感；不能用 normcase 后的
            # 路径字符串判同，否则同一父目录下不同 FileId 的 old/OLD 会被
            # 假报为零差异。samefile 使用实际文件系统身份，同时仍兼容 x/.
            self._same_source = os.path.samefile(source_old, source_new)
        except OSError:
            # 缺失/不可访问端点由后续快照给出原有的明确错误。
            self._same_source = False

        super().__init__(new_dir)  # 报告仍显示用户选择的新版本目录
        self.source_old_dir = old_dir
        self.source_new_dir = new_dir
        self._owned_temp_dirs = []
        self._snapshot_requested = snapshot
        self._snapshot_lock = threading.Lock()
        self._snapshotted = not snapshot
        self._captured_changed_files = None
        self._captured_old_directories = set()
        self._defer_snapshot_final_verification = False
        self._snapshot_avoid_paths = (source_old, source_new)
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
                # 两端都先固定身份和路径集合，再开始复制，避免复制旧端期间
                # 新端新增/替换的文件被悄悄纳入同一次任务。
                old_capture = self._capture_directory(self.source_old_dir)
                # 同一目录作为两个端点时，它们表示同一个稳定状态；
                # 只捕获一次并复用，避免两次扫描之间的变化被伪造成差异。
                new_capture = (
                    old_capture
                    if self._same_source
                    else self._capture_directory(self.source_new_dir)
                )
                captures = (old_capture,) if self._same_source else (
                    old_capture, new_capture
                )
                if self.MAX_SNAPSHOT_FILES is not None:
                    total_files = sum(len(capture["files"]) for capture in captures)
                    if total_files > self.MAX_SNAPSHOT_FILES:
                        raise RuntimeError(
                            f"文件夹快照文件数超过上限: {total_files} > "
                            f"{self.MAX_SNAPSHOT_FILES}"
                        )
                if self.MAX_SNAPSHOT_TOTAL_BYTES is not None:
                    total_bytes = sum(
                        signature[1]
                        for capture in captures
                        for signature in capture["file_signatures"].values()
                    )
                    if total_bytes > self.MAX_SNAPSHOT_TOTAL_BYTES:
                        raise RuntimeError(
                            f"文件夹快照总字节数超过上限: {total_bytes} > "
                            f"{self.MAX_SNAPSHOT_TOTAL_BYTES}"
                        )

                changed_files = (
                    []
                    if self._same_source
                    else self._compare_captures(old_capture, new_capture)
                )
                old_snapshot_files = {
                    item.path for item in changed_files
                    if item.change_type in (ChangeType.DELETED, ChangeType.MODIFIED)
                }
                new_snapshot_files = {
                    item.path for item in changed_files
                    if item.change_type in (ChangeType.ADDED, ChangeType.MODIFIED)
                }
                old_snapshot_bytes = sum(
                    old_capture["file_signatures"][path][1]
                    for path in old_snapshot_files
                )
                new_snapshot_bytes = sum(
                    new_capture["file_signatures"][path][1]
                    for path in new_snapshot_files
                )

                # 先固定整棵树的身份并完成内容比较，只把报告和导出真正会读取
                # 的变更端点复制到独立临时根。这样稳定性不降级，同时磁盘需求
                # 从“两棵完整目录”收敛到“变更文件的两端”。
                old_snapshot_capture = dict(old_capture)
                old_snapshot_capture["snapshot_files"] = old_snapshot_files
                new_snapshot_capture = dict(new_capture)
                new_snapshot_capture["snapshot_files"] = new_snapshot_files
                self._defer_snapshot_final_verification = True
                old_snapshot = self._snapshot_directory(
                    self.source_old_dir,
                    "comparetool_folder_old_",
                    old_snapshot_capture,
                    old_snapshot_bytes + new_snapshot_bytes,
                )
                new_snapshot = (
                    old_snapshot
                    if self._same_source
                    else self._snapshot_directory(
                        self.source_new_dir,
                        "comparetool_folder_new_",
                        new_snapshot_capture,
                        new_snapshot_bytes,
                    )
                )
                self._verify_capture(self.source_old_dir, old_capture)
                if not self._same_source:
                    self._verify_capture(self.source_new_dir, new_capture)
            except BaseException:
                self.cleanup()
                self.old_dir = self.source_old_dir
                self.new_dir = self.source_new_dir
                raise
            finally:
                self._defer_snapshot_final_verification = False
            self.old_dir = old_snapshot
            self.new_dir = new_snapshot
            self._captured_changed_files = list(changed_files)
            self._captured_old_directories = set(old_capture["directories"])
            self._snapshotted = True

    def _should_prune_directory(self, relative_path: str) -> bool:
        """仅当规则明确覆盖任意深度后代时才剪枝，避免误伤 foo/*。"""
        return bool(self.exclude_patterns) and self._is_excluded_tree(relative_path)

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
                    if (
                        self.MAX_SNAPSHOT_ENTRIES is not None
                        and len(files) + len(directories) > self.MAX_SNAPSHOT_ENTRIES
                    ):
                        raise RuntimeError(
                            "文件夹快照目录和文件条目数超过上限: "
                            f"{self.MAX_SNAPSHOT_ENTRIES}"
                        )
                    continue
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接或联接点，已拒绝读取: {full}")
                directories.add(rel)
                if (
                    self.MAX_SNAPSHOT_ENTRIES is not None
                    and len(files) + len(directories) > self.MAX_SNAPSHOT_ENTRIES
                ):
                    raise RuntimeError(
                        "文件夹快照目录和文件条目数超过上限: "
                        f"{self.MAX_SNAPSHOT_ENTRIES}"
                    )
            for f in filenames:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if apply_excludes and self._is_excluded(rel):
                    continue
                if is_link_or_junction(full):
                    raise RuntimeError(f"比对目录包含符号链接，已拒绝读取: {full}")
                files.add(rel)
                if (
                    self.MAX_SNAPSHOT_FILES is not None
                    and len(files) > self.MAX_SNAPSHOT_FILES
                ):
                    raise RuntimeError(
                        f"文件夹快照文件数超过上限: {self.MAX_SNAPSHOT_FILES}"
                    )
                if (
                    self.MAX_SNAPSHOT_ENTRIES is not None
                    and len(files) + len(directories) > self.MAX_SNAPSHOT_ENTRIES
                ):
                    raise RuntimeError(
                        "文件夹快照目录和文件条目数超过上限: "
                        f"{self.MAX_SNAPSHOT_ENTRIES}"
                    )
        return files, directories

    def _file_signature(self, path: str):
        if is_link_or_junction(path):
            raise RuntimeError(f"比对目录包含符号链接或联接点，已拒绝读取: {path}")
        try:
            return regular_file_path_identity(path)
        except OSError as exc:
            raise RuntimeError(f"读取比对源文件元数据失败: {path}: {exc}") from exc
        except RuntimeError as exc:
            raise RuntimeError(f"比对源包含非普通文件，已拒绝读取: {path}") from exc

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

    def _capture_directory(self, source: str) -> dict:
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
        return {
            "root_identity": root_identity,
            "files": files,
            "directories": directories,
            "directory_identities": initial_directory_identities,
            "file_signatures": initial_file_signatures,
        }

    def _compare_captures(self, old_capture: dict, new_capture: dict):
        old_files = old_capture["files"]
        new_files = new_capture["files"]
        result = [
            ChangedFile(path=path, change_type=ChangeType.ADDED)
            for path in sorted(new_files - old_files)
        ]
        result.extend(
            ChangedFile(path=path, change_type=ChangeType.DELETED)
            for path in sorted(old_files - new_files)
        )
        for path in sorted(old_files & new_files):
            if not self._same_captured_file_content(
                self.source_old_dir,
                self.source_new_dir,
                path,
                old_capture["file_signatures"][path],
                new_capture["file_signatures"][path],
            ):
                result.append(ChangedFile(path=path, change_type=ChangeType.MODIFIED))
        return result

    def _same_captured_file_content(
        self,
        old_source: str,
        new_source: str,
        relative_path: str,
        old_signature: tuple,
        new_signature: tuple,
    ) -> bool:
        if old_signature[1] != new_signature[1]:
            return False

        old_path = self._resolve_file_path(old_source, relative_path)
        new_path = self._resolve_file_path(new_source, relative_path)
        try:
            with open_regular_file_no_links(old_path) as old_stream, \
                    open_regular_file_no_links(new_path) as new_stream:
                if regular_file_handle_identity(old_stream) != old_signature:
                    raise RuntimeError(
                        f"旧版本文件在快照复制前发生变化: {old_path}"
                    )
                if regular_file_handle_identity(new_stream) != new_signature:
                    raise RuntimeError(
                        f"新版本文件在快照复制前发生变化: {new_path}"
                    )
                same = True
                while True:
                    old_chunk = old_stream.read(1024 * 1024)
                    new_chunk = new_stream.read(1024 * 1024)
                    if old_chunk != new_chunk:
                        same = False
                        break
                    if not old_chunk:
                        break
                if regular_file_handle_identity(old_stream) != old_signature:
                    raise RuntimeError(
                        f"旧版本文件在快照复制期间发生变化: {old_path}"
                    )
                if regular_file_handle_identity(new_stream) != new_signature:
                    raise RuntimeError(
                        f"新版本文件在快照复制期间发生变化: {new_path}"
                    )
        except OSError as exc:
            raise RuntimeError(
                f"读取文件夹端点内容失败: {relative_path}: {exc}"
            ) from exc

        if self._file_signature(old_path) != old_signature:
            raise RuntimeError(f"旧版本文件在快照复制期间发生变化: {old_path}")
        if self._file_signature(new_path) != new_signature:
            raise RuntimeError(f"新版本文件在快照复制期间发生变化: {new_path}")
        return same

    def _verify_capture(self, source: str, capture: dict):
        final_files, final_directories = self._walk_tree(
            source, apply_excludes=True
        )
        if (
            final_files != capture["files"]
            or final_directories != capture["directories"]
        ):
            raise RuntimeError(f"比对源目录路径集合在快照期间发生变化: {source}")
        if self._directory_identity(source) != capture["root_identity"]:
            raise RuntimeError(f"比对源目录在快照期间被替换: {source}")
        for relative_path, expected_identity in capture[
            "directory_identities"
        ].items():
            current_path = self._resolve_file_path(source, relative_path)
            if self._directory_identity(current_path) != expected_identity:
                raise RuntimeError(f"比对源目录在快照期间被替换: {current_path}")
        for relative_path, expected_signature in capture["file_signatures"].items():
            current_path = self._resolve_file_path(source, relative_path)
            if self._file_signature(current_path) != expected_signature:
                raise RuntimeError(
                    f"比对源文件在快照期间发生变化: {current_path}"
                )

    def _snapshot_directory(
        self,
        source: str,
        prefix: str,
        capture: dict,
        required_free_bytes: int,
    ) -> str:
        files = capture["files"]
        initial_file_signatures = capture["file_signatures"]
        selected_files = capture.get("snapshot_files", files)
        target = create_temp_dir(
            prefix=prefix,
            avoid_paths=self._snapshot_avoid_paths,
            required_free_bytes=(
                required_free_bytes + self.MIN_SNAPSHOT_FREE_BYTES
            ),
        )
        self._owned_temp_dirs.append(target)
        for rel_path in sorted(selected_files):
            source_path = self._resolve_file_path(source, rel_path)
            target_path = self._resolve_file_path(target, rel_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            expected_signature = initial_file_signatures[rel_path]
            try:
                with open_regular_file_no_links(source_path) as src, open(target_path, "wb") as dst:
                    opened_signature = regular_file_handle_identity(src)
                    if opened_signature != expected_signature:
                        raise RuntimeError(
                            f"比对源文件在快照复制前发生变化: {source_path}"
                        )
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    copied_size = dst.tell()
                    closed_signature = regular_file_handle_identity(src)
            except OSError as exc:
                raise RuntimeError(f"复制比对源文件失败: {source_path}: {exc}") from exc
            if (
                copied_size != expected_signature[1]
                or closed_signature != expected_signature
                or self._file_signature(source_path) != expected_signature
            ):
                raise RuntimeError(
                    f"比对源文件在快照复制期间发生变化: {source_path}"
                )

        if not self._defer_snapshot_final_verification:
            self._verify_capture(source, capture)
        return target

    def get_changed_files(self, old_version: str = "", new_version: str = "") -> List[ChangedFile]:
        """对比两个文件夹，返回差异文件列表"""
        self._ensure_snapshot()
        if self._captured_changed_files is not None:
            result = list(self._captured_changed_files)
            old_dirs = set(self._captured_old_directories)
        else:
            old_files, old_dirs = self._walk_tree(self.old_dir)
            new_files, _new_dirs = self._walk_tree(self.new_dir)

            result = []

            for f in new_files - old_files:
                result.append(ChangedFile(path=f, change_type=ChangeType.ADDED))

            for f in old_files - new_files:
                result.append(ChangedFile(path=f, change_type=ChangeType.DELETED))

            for f in old_files & new_files:
                # snapshot=False 只用于已经由上层固定的端点目录。
                if not self._same_file_content(
                    os.path.join(self.old_dir, f), os.path.join(self.new_dir, f)
                ):
                    result.append(ChangedFile(path=f, change_type=ChangeType.MODIFIED))

        new_endpoint_files = {
            item.path for item in result
            if item.change_type in (ChangeType.ADDED, ChangeType.RENAMED)
        }
        self.required_directory_deletions = windows_directories_replaced_by_files(
            old_dirs, new_endpoint_files
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

    def get_known_file_raw_size(self, version: str, file_path: str):
        return self.get_file_size(version, file_path)

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
