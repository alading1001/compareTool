import os
import shutil
import tempfile
import zipfile
import tarfile
from typing import List

from .base import BaseVCS, ChangedFile
from .folder_vcs import FolderVCS


class ArchiveVCS(BaseVCS):
    """压缩包比对实现：解压到临时目录，委托 FolderVCS 做比对"""

    def __init__(self, old_archive: str, new_archive: str):
        self.old_archive = old_archive
        self.new_archive = new_archive
        self._tmp_old = tempfile.mkdtemp(prefix="cmp_old_")
        self._tmp_new = tempfile.mkdtemp(prefix="cmp_new_")
        self._extract(old_archive, self._tmp_old)
        self._extract(new_archive, self._tmp_new)
        self._folder = FolderVCS(self._tmp_old, self._tmp_new)
        super().__init__(self._tmp_new)

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
            raise ValueError(f"不支持的压缩格式: {archive_path}（支持 .zip / .tar / .tar.gz / .tar.bz2）")

    def _extract_zip(self, path: str, dest: str):
        with zipfile.ZipFile(path, 'r') as zf:
            for info in zf.infolist():
                name = self._fix_zip_filename(info)
                target = os.path.join(dest, name)
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
            tf.extractall(dest)

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
        for d in (self._tmp_old, self._tmp_new):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
