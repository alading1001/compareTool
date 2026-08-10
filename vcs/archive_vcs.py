import os
import posixpath
import shutil
import stat
import zipfile
import tarfile
from typing import List

from path_safety import safe_join
from .base import BaseVCS, ChangedFile
from .folder_vcs import FolderVCS
from .temp_storage import create_temp_dir


class ArchiveVCS(BaseVCS):
    """压缩包比对实现：解压到临时目录，委托 FolderVCS 做比对"""

    MAX_ARCHIVE_MEMBERS = 100_000
    MAX_SINGLE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
    MAX_TOTAL_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024
    MAX_COMPRESSION_RATIO = 1000

    def __init__(self, old_archive: str, new_archive: str):
        self.old_archive = old_archive
        self.new_archive = new_archive
        self._tmp_old = ""
        self._tmp_new = ""
        self._folder = None
        try:
            self._tmp_old = create_temp_dir(prefix="cmp_old_")
            self._tmp_new = create_temp_dir(prefix="cmp_new_")
            self._extract(old_archive, self._tmp_old)
            self._extract(new_archive, self._tmp_new)
            self._folder = FolderVCS(self._tmp_old, self._tmp_new)
            super().__init__(self._tmp_new)
        except Exception:
            self.cleanup()
            raise

    # ── 压缩包解压 ──

    @staticmethod
    def _is_zip(path: str) -> bool:
        return path.lower().endswith(('.zip', '.jar', '.war', '.ear', '.aar'))

    @staticmethod
    def _is_tar(path: str) -> bool:
        return path.lower().endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2'))

    def _extract(self, archive_path: str, dest: str):
        if self._is_zip(archive_path):
            self._extract_zip(archive_path, dest)
        elif self._is_tar(archive_path):
            self._extract_tar(archive_path, dest)
        else:
            raise ValueError(
                f"不支持的压缩格式: {archive_path}"
                "（支持 .zip / .jar / .war / .ear / .aar / .tar / .tar.gz / .tgz / .tar.bz2 / .tbz2）"
            )

    def _extract_zip(self, path: str, dest: str):
        with zipfile.ZipFile(path, 'r') as zf:
            members = zf.infolist()
            self._validate_archive_limits(
                path,
                [(info.filename, info.file_size, info.compress_size, info.is_dir()) for info in members],
            )
            for info in members:
                name = self._fix_zip_filename(info)
                if self._is_root_directory(name, info.is_dir()):
                    continue
                target = self._safe_extract_target(dest, name)
                unix_mode = info.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise ValueError(f"压缩包包含不安全的符号链接: {name}")
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, 'wb') as dst:
                        shutil.copyfileobj(src, dst)

    def _extract_tar(self, path: str, dest: str):
        mode = 'r:gz' if path.lower().endswith(('.gz', '.tgz')) else \
               'r:bz2' if path.lower().endswith(('.bz2', '.tbz2')) else 'r'
        with tarfile.open(path, mode) as tf:
            members = tf.getmembers()
            archive_size = max(os.path.getsize(path), 1)
            self._validate_archive_limits(
                path,
                [(member.name, member.size, archive_size, member.isdir()) for member in members],
                check_member_ratio=False,
            )
            total_size = sum(member.size for member in members if member.isfile())
            if total_size / archive_size > self.MAX_COMPRESSION_RATIO:
                raise ValueError(
                    f"压缩包展开比例过高，已拒绝解压: {path} "
                    f"({total_size / archive_size:.0f}:1)"
                )
            for member in members:
                if self._is_root_directory(member.name, member.isdir()):
                    continue
                target = self._safe_extract_target(dest, member.name)
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError(f"压缩包包含不安全或不支持的链接/特殊文件: {member.name}")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    raise ValueError(f"无法读取压缩包成员: {member.name}")
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    @staticmethod
    def _safe_extract_target(dest: str, member_name: str) -> str:
        """解析压缩包成员路径，并保证最终目标仍位于临时解压目录内。"""
        name = (member_name or "").replace("\\", "/")
        try:
            return safe_join(dest, name, label="压缩包成员路径")
        except ValueError as exc:
            raise ValueError(f"压缩包包含不安全路径: {member_name}（{exc}）") from exc

    @staticmethod
    def _is_root_directory(member_name: str, is_dir: bool) -> bool:
        normalized = posixpath.normpath((member_name or "").replace("\\", "/"))
        return is_dir and normalized == "."

    @classmethod
    def _validate_archive_limits(cls, path: str, members, check_member_ratio: bool = True):
        if len(members) > cls.MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                f"压缩包成员过多，已拒绝解压: {len(members)} > {cls.MAX_ARCHIVE_MEMBERS}"
            )

        total_size = 0
        for name, size, compressed_size, is_dir in members:
            if is_dir:
                continue
            if size < 0:
                raise ValueError(f"压缩包成员大小无效: {name}")
            if size > cls.MAX_SINGLE_MEMBER_BYTES:
                raise ValueError(
                    f"压缩包单个成员展开后过大，已拒绝解压: {name} ({size} 字节)"
                )
            total_size += size
            if total_size > cls.MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ValueError(
                    "压缩包累计展开大小超过限制，已拒绝解压: "
                    f"{total_size} > {cls.MAX_TOTAL_UNCOMPRESSED_BYTES} 字节"
                )
            if check_member_ratio and size > 0:
                ratio = size / max(compressed_size, 1)
                if ratio > cls.MAX_COMPRESSION_RATIO:
                    raise ValueError(
                        f"压缩包成员展开比例过高，已拒绝解压: {name} ({ratio:.0f}:1)"
                    )

    @staticmethod
    def _fix_zip_filename(info: zipfile.ZipInfo) -> str:
        """修正 ZIP 文件名编码：CP437 编码的中文 zip → GBK 解码"""
        name = info.filename
        if info.flag_bits & 0x800:
            return name  # UTF-8 标志位已设置，无需修正
        # 没设 UTF-8 标志，尝试还原原始字节再按 GBK 解码
        try:
            raw = name.encode('cp437')
            try:
                return raw.decode('gbk')
            except UnicodeDecodeError:
                pass
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return name

    # ── BaseVCS 接口，全部委托给 FolderVCS ──

    def set_exclude_patterns(self, patterns: List[str]):
        super().set_exclude_patterns(patterns)
        self._folder.set_exclude_patterns(patterns)

    def get_changed_files(self, old_version: str = "", new_version: str = "") -> List[ChangedFile]:
        return self._folder.get_changed_files("old", "new")

    def get_file_content(self, version: str, file_path: str) -> str:
        return self._folder.get_file_content(self._to_folder_ver(version), file_path)

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        return self._folder.get_file_content_bytes(self._to_folder_ver(version), file_path)

    def get_file_content_raw_bytes(self, version: str, file_path: str) -> bytes:
        return self._folder.get_file_content_raw_bytes(self._to_folder_ver(version), file_path)

    def _to_folder_ver(self, version: str) -> str:
        """将外部版本标识（zip 路径）转为 FolderVCS 能识别的 'old'/'new'"""
        if version in ("old", "new"):
            return version
        if version == self.old_archive:
            return "old"
        if version == self.new_archive:
            return "new"
        # 兜底：与临时目录比对
        if version == self._tmp_old:
            return "old"
        return "new"

    def get_file_content_working(self, file_path: str) -> str:
        return self._folder.get_file_content_working(file_path)

    def get_file_content_bytes_working(self, file_path: str) -> bytes:
        return self._folder.get_file_content_bytes_working(file_path)

    def get_versions(self) -> List[str]:
        return []

    def check_version_exists(self, version: str) -> bool:
        return True

    # ── 清理 ──

    def cleanup(self):
        """删除临时解压目录"""
        for d in (getattr(self, "_tmp_old", ""), getattr(self, "_tmp_new", "")):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
