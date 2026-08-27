import os
import posixpath
import shutil
import stat
import bz2
import gzip
import zipfile
import tarfile
from typing import List

from path_safety import safe_join
from .base import BaseVCS, ChangedFile, ChangeType
from .folder_vcs import FolderVCS
from .temp_storage import create_temp_dir, remove_temp_dir
from logger import warn


class ArchiveVCS(BaseVCS):
    """压缩包比对实现：解压到临时目录，委托 FolderVCS 做比对"""

    MAX_ARCHIVE_MEMBERS = 100_000
    MAX_SINGLE_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
    MAX_TOTAL_UNCOMPRESSED_BYTES = 10 * 1024 * 1024 * 1024
    MAX_COMPRESSION_RATIO = 1000
    MAX_TAR_METADATA_BYTES = 1024 * 1024

    def __init__(self, old_archive: str, new_archive: str):
        self.old_archive = old_archive
        self.new_archive = new_archive
        self._tmp_old = ""
        self._tmp_new = ""
        self._folder = None
        self._old_metadata = {}
        self._new_metadata = {}
        try:
            self._tmp_old = create_temp_dir(prefix="cmp_old_")
            self._tmp_new = create_temp_dir(prefix="cmp_new_")
            self._extract(old_archive, self._tmp_old, self._old_metadata)
            self._extract(new_archive, self._tmp_new, self._new_metadata)
            self._folder = FolderVCS(self._tmp_old, self._tmp_new, snapshot=False)
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

    def _extract(self, archive_path: str, dest: str, metadata: dict):
        if self._is_zip(archive_path):
            self._extract_zip(archive_path, dest, metadata)
        elif self._is_tar(archive_path):
            self._extract_tar(archive_path, dest, metadata)
        else:
            raise ValueError(
                f"不支持的压缩格式: {archive_path}"
                "（支持 .zip / .jar / .war / .ear / .aar / .tar / .tar.gz / .tgz / .tar.bz2 / .tbz2）"
            )

    def _extract_zip(self, path: str, dest: str, metadata: dict = None):
        metadata = metadata if metadata is not None else {}
        with zipfile.ZipFile(path, 'r') as zf:
            members = zf.infolist()
            self._validate_archive_limits(
                path,
                [(info.filename, info.file_size, info.compress_size, info.is_dir()) for info in members],
            )
            decoded_members = [
                (info, self._fix_zip_filename(info)) for info in members
            ]
            self._validate_archive_targets(
                dest,
                [(name, info.is_dir()) for info, name in decoded_members],
            )
            for info, name in decoded_members:
                unix_mode = info.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if stat.S_ISLNK(unix_mode):
                    raise ValueError(f"压缩包包含不安全的符号链接: {name}")
                if file_type and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
                    raise ValueError(f"压缩包包含不安全或不支持的特殊文件: {name}")
            for info, name in decoded_members:
                if self._is_root_directory(name, info.is_dir()):
                    continue
                target = self._safe_extract_target(dest, name)
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    unix_mode = info.external_attr >> 16
                    if info.create_system == 3 and stat.S_ISREG(unix_mode):
                        relative = os.path.relpath(target, dest).replace("\\", "/")
                        metadata[relative] = {
                            "mode": f"{stat.S_IMODE(unix_mode):04o}",
                            "executable": bool(unix_mode & 0o111),
                        }

    def _extract_tar(self, path: str, dest: str, metadata: dict = None):
        metadata = metadata if metadata is not None else {}
        self._preflight_tar(path, dest)
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
            self._validate_archive_targets(
                dest,
                [(member.name, member.isdir()) for member in members],
            )
            for member in members:
                if self._is_root_directory(member.name, member.isdir()):
                    continue
                if not (member.isdir() or member.isfile()):
                    raise ValueError(
                        f"压缩包包含不安全或不支持的链接/特殊文件: {member.name}"
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
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    raise ValueError(f"无法读取压缩包成员: {member.name}")
                with src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                relative = os.path.relpath(target, dest).replace("\\", "/")
                metadata[relative] = {
                    "mode": f"{stat.S_IMODE(member.mode):04o}",
                    "executable": bool(member.mode & 0o111),
                }

    @classmethod
    def _preflight_tar(cls, path: str, dest: str):
        """在 tarfile 解析 PAX/GNU 扩展前先做有界流式头检查。

        tarfile.getmembers() 会先把长文件名/PAX 载荷整体读入内存；这里先从
        原始解压流限制这些隐藏元数据，再允许标准库执行第二遍实际解压。
        """
        lower = path.lower()
        opener = gzip.open if lower.endswith((".gz", ".tgz")) else (
            bz2.open if lower.endswith((".bz2", ".tbz2")) else open
        )
        targets = []
        total_size = 0
        header_count = 0
        pending_name = None
        pending_pax = {}
        global_pax = {}
        with opener(path, "rb") as stream:
            while True:
                header = cls._read_exact(stream, 512, allow_eof=True)
                if not header or header == b"\0" * 512:
                    break
                header_count += 1
                if header_count > cls.MAX_ARCHIVE_MEMBERS * 2 + 1024:
                    raise ValueError("tar 头记录过多，已拒绝解压")
                try:
                    member = tarfile.TarInfo.frombuf(
                        header, tarfile.ENCODING, "surrogateescape"
                    )
                except (tarfile.TarError, ValueError) as exc:
                    raise ValueError(f"tar 头损坏，已拒绝解压: {path}") from exc
                size = member.size
                if size < 0:
                    raise ValueError(f"压缩包成员大小无效: {member.name}")
                if member.type in (
                    tarfile.GNUTYPE_LONGNAME,
                    tarfile.GNUTYPE_LONGLINK,
                    tarfile.XHDTYPE,
                    tarfile.XGLTYPE,
                ):
                    if size > cls.MAX_TAR_METADATA_BYTES:
                        raise ValueError(
                            f"tar 扩展元数据过大，已拒绝解压: {member.name} ({size} 字节)"
                        )
                    payload = cls._read_exact(stream, size)
                    cls._skip_tar_padding(stream, size)
                    if member.type == tarfile.GNUTYPE_LONGNAME:
                        pending_name = payload.rstrip(b"\0\n").decode(
                            tarfile.ENCODING, errors="surrogateescape"
                        )
                    elif member.type == tarfile.GNUTYPE_LONGLINK:
                        # 后续实体仍会因链接类型被拒绝；只需保证载荷有界。
                        pass
                    else:
                        parsed = cls._parse_pax_payload(payload)
                        if member.type == tarfile.XGLTYPE:
                            global_pax.update(parsed)
                        else:
                            pending_pax.update(parsed)
                    continue

                effective = dict(global_pax)
                effective.update(pending_pax)
                name = effective.get("path") or pending_name or member.name
                pending_name = None
                pending_pax = {}
                if "size" in effective:
                    try:
                        size = int(effective["size"])
                    except ValueError as exc:
                        raise ValueError(f"tar PAX size 无效: {name}") from exc
                is_dir = member.isdir()
                if not (is_dir or member.isfile()):
                    raise ValueError(f"压缩包包含不安全或不支持的链接/特殊文件: {name}")
                if not is_dir:
                    if size > cls.MAX_SINGLE_MEMBER_BYTES:
                        raise ValueError(
                            f"压缩包单个成员展开后过大，已拒绝解压: {name} ({size} 字节)"
                        )
                    total_size += size
                    if total_size > cls.MAX_TOTAL_UNCOMPRESSED_BYTES:
                        raise ValueError("压缩包累计展开大小超过限制，已拒绝解压")
                targets.append((name, is_dir))
                if len(targets) > cls.MAX_ARCHIVE_MEMBERS:
                    raise ValueError("压缩包成员过多，已拒绝解压")
                cls._discard_exact(stream, size)
                cls._skip_tar_padding(stream, size)
        cls._validate_archive_targets(dest, targets)
        archive_size = max(os.path.getsize(path), 1)
        if total_size / archive_size > cls.MAX_COMPRESSION_RATIO:
            raise ValueError(
                f"压缩包展开比例过高，已拒绝解压: {path} "
                f"({total_size / archive_size:.0f}:1)"
            )

    @staticmethod
    def _read_exact(stream, size: int, allow_eof: bool = False) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                if allow_eof and remaining == size:
                    return b""
                raise ValueError("tar 数据提前结束，已拒绝解压")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @classmethod
    def _skip_tar_padding(cls, stream, size: int):
        padding = (-size) % 512
        if padding:
            cls._read_exact(stream, padding)

    @staticmethod
    def _discard_exact(stream, size: int):
        remaining = size
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("tar 数据提前结束，已拒绝解压")
            remaining -= len(chunk)

    @staticmethod
    def _parse_pax_payload(payload: bytes) -> dict:
        result = {}
        offset = 0
        while offset < len(payload):
            space = payload.find(b" ", offset)
            if space <= offset:
                raise ValueError("tar PAX 记录格式无效")
            try:
                length = int(payload[offset:space])
            except ValueError as exc:
                raise ValueError("tar PAX 记录长度无效") from exc
            if length <= 0 or offset + length > len(payload):
                raise ValueError("tar PAX 记录越界")
            record = payload[space + 1:offset + length]
            if not record.endswith(b"\n") or b"=" not in record:
                raise ValueError("tar PAX 记录格式无效")
            key, value = record[:-1].split(b"=", 1)
            result[key.decode("utf-8", errors="strict")] = value.decode(
                "utf-8", errors="strict"
            )
            offset += length
        return result

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
    def _validate_archive_targets(cls, dest: str, members):
        """在写盘前拒绝重复、大小写碰撞和文件/目录前缀冲突。"""
        targets = {}
        for name, is_dir in members:
            if cls._is_root_directory(name, is_dir):
                continue
            target = cls._safe_extract_target(dest, name)
            relative = os.path.relpath(target, os.path.abspath(dest)).replace("\\", "/")
            parts = tuple(part.casefold() for part in relative.split("/"))
            normalized_name = relative.replace("\\", "/")
            previous = targets.get(parts)
            if previous is not None:
                previous_name, previous_is_dir, previous_normalized = previous
                if is_dir and previous_is_dir and normalized_name == previous_normalized:
                    continue
                raise ValueError(
                    "压缩包成员会写入同一 Windows 路径，已拒绝解压: "
                    f"{previous_name} / {name}"
                )
            targets[parts] = (name, is_dir, normalized_name)

        ordered = sorted(targets.items(), key=lambda item: item[0])
        for index, (parts, (name, is_dir, _normalized)) in enumerate(ordered[:-1]):
            if is_dir:
                continue
            other_parts, (other_name, _other_is_dir, _other_normalized) = ordered[index + 1]
            if len(other_parts) > len(parts) and other_parts[:len(parts)] == parts:
                raise ValueError(
                    "压缩包成员存在文件/目录前缀冲突，已拒绝解压: "
                    f"{name} / {other_name}"
                )

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
        files = self._folder.get_changed_files("old", "new")
        by_path = {item.path: item for item in files}
        for path in sorted(set(self._old_metadata) | set(self._new_metadata)):
            old = self._old_metadata.get(path)
            new = self._new_metadata.get(path)
            item = by_path.get(path)
            if item is None and old != new:
                item = ChangedFile(path=path, change_type=ChangeType.MODIFIED)
                files.append(item)
                by_path[path] = item
            if item is None:
                continue
            item.old_executable = old.get("executable") if old else None
            item.new_executable = new.get("executable") if new else None
            item.old_mode = old.get("mode", "") if old else ""
            item.new_mode = new.get("mode", "") if new else ""
            if old and new and old != new:
                item.metadata_changes.append(
                    f"压缩包文件模式：{old['mode']} → {new['mode']}"
                )
        return self._filter_files(files)

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
                try:
                    remove_temp_dir(d)
                except OSError as exc:
                    warn(f"清理压缩包临时目录失败，已保留现场: {d}: {exc}")

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
