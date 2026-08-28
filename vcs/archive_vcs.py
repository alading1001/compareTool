import os
import posixpath
import shutil
import stat
import bz2
import gzip
import zipfile
import tarfile
import struct
from typing import List

from path_safety import (
    is_link_or_junction,
    open_regular_file_no_links,
    regular_file_handle_identity,
    regular_file_path_identity,
    safe_join,
)
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
    MAX_TAR_METADATA_RECORDS = 4096
    MAX_TAR_PAX_FIELDS = 4096
    MAX_ZIP_CENTRAL_DIRECTORY_BYTES = 256 * 1024 * 1024
    MAX_ARCHIVE_SOURCE_BYTES = 20 * 1024 * 1024 * 1024
    MIN_WORKING_FREE_BYTES = 1024 * 1024 * 1024

    _ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
    _ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
    _ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
    _ZIP_CENTRAL_SIGNATURE = b"PK\x01\x02"
    _ZIP_DIGITAL_SIGNATURE = b"PK\x05\x05"

    def __init__(self, old_archive: str, new_archive: str):
        self.old_archive = old_archive
        self.new_archive = new_archive
        self._tmp_old = ""
        self._tmp_new = ""
        self._tmp_archive_sources = ""
        self._folder = None
        self._old_metadata = {}
        self._new_metadata = {}
        try:
            self._tmp_archive_sources = create_temp_dir(prefix="cmp_old_archive_")
            old_capture = self._capture_archive_source(old_archive)
            new_capture = self._capture_archive_source(new_archive)
            source_bytes = old_capture["size"] + new_capture["size"]
            if source_bytes > self.MAX_ARCHIVE_SOURCE_BYTES:
                raise RuntimeError(
                    f"压缩包源文件总大小超过上限: {source_bytes} > "
                    f"{self.MAX_ARCHIVE_SOURCE_BYTES}"
                )
            self._ensure_free_space(self._tmp_archive_sources, source_bytes)
            old_snapshot = self._snapshot_archive(old_capture, "old")
            new_snapshot = self._snapshot_archive(new_capture, "new")
            self._tmp_old = create_temp_dir(prefix="cmp_old_")
            self._tmp_new = create_temp_dir(prefix="cmp_new_")
            self._extract(old_snapshot, self._tmp_old, self._old_metadata)
            self._extract(new_snapshot, self._tmp_new, self._new_metadata)
            self._folder = FolderVCS(self._tmp_old, self._tmp_new, snapshot=False)
            super().__init__(self._tmp_new)
        except Exception:
            self.cleanup()
            raise

    # ── 压缩包解压 ──

    @classmethod
    def _archive_suffix(cls, path: str) -> str:
        lower = path.lower()
        for suffix in (
            ".tar.gz", ".tar.bz2", ".zip", ".jar", ".war", ".ear",
            ".aar", ".tgz", ".tbz2", ".tar",
        ):
            if lower.endswith(suffix):
                return suffix
        return os.path.splitext(path)[1]

    def _capture_archive_source(self, source: str) -> dict:
        source = os.path.abspath(source)
        if is_link_or_junction(source):
            raise RuntimeError(f"不允许将符号链接或联接点作为压缩包源: {source}")
        try:
            signature = regular_file_path_identity(source)
        except OSError as exc:
            raise RuntimeError(f"无法读取压缩包源: {source}: {exc}") from exc
        except RuntimeError as exc:
            raise RuntimeError(f"压缩包源不是普通文件: {source}") from exc
        return {
            "path": source,
            "signature": signature,
            "size": signature[1],
        }

    def _snapshot_archive(self, capture: dict, label: str) -> str:
        """按两端复制前共同捕获的身份快照固定归档源。"""
        source = capture["path"]
        expected_signature = capture["signature"]
        try:
            path_before = regular_file_path_identity(source)
        except OSError as exc:
            raise RuntimeError(f"无法读取压缩包源: {source}: {exc}") from exc
        if (
            is_link_or_junction(source)
            or path_before != expected_signature
        ):
            raise RuntimeError(f"压缩包源在成对捕获后发生变化，已中止: {source}")

        target = os.path.join(
            self._tmp_archive_sources,
            f"{label}{self._archive_suffix(source)}",
        )
        copied = 0
        with open_regular_file_no_links(source) as src, open(target, "xb") as dst:
            handle_before = regular_file_handle_identity(src)
            if handle_before != expected_signature:
                raise RuntimeError(f"压缩包源在打开前已发生变化: {source}")
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
                copied += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
            handle_after = regular_file_handle_identity(src)

        try:
            path_after = regular_file_path_identity(source)
        except OSError as exc:
            raise RuntimeError(f"压缩包源在快照期间消失: {source}: {exc}") from exc
        if (
            path_before != path_after
            or handle_before != handle_after
            or copied != expected_signature[1]
            or is_link_or_junction(source)
        ):
            raise RuntimeError(f"压缩包源在快照期间发生变化，已中止: {source}")
        return target

    def _ensure_free_space(self, path: str, payload_bytes: int):
        free_bytes = shutil.disk_usage(path).free
        required = payload_bytes + self.MIN_WORKING_FREE_BYTES
        if free_bytes < required:
            raise RuntimeError(
                "压缩包任务可用磁盘空间不足，已中止生成: "
                f"需要至少 {required} 字节，当前 {free_bytes} 字节"
            )

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
        self._preflight_zip(path)
        with zipfile.ZipFile(path, 'r') as zf:
            members = zf.infolist()
            total_size = self._validate_archive_limits(
                path,
                [(info.filename, info.file_size, info.compress_size, info.is_dir()) for info in members],
            )
            self._ensure_free_space(dest, total_size)
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
                    file_type = stat.S_IFMT(unix_mode)
                    if (
                        info.create_system == 3
                        and (file_type == 0 or stat.S_ISREG(unix_mode))
                    ):
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
            total_size = self._validate_archive_limits(
                path,
                [(member.name, member.size, archive_size, member.isdir()) for member in members],
                check_member_ratio=False,
            )
            self._ensure_free_space(dest, total_size)
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
        metadata_size = 0
        metadata_records = 0
        pending_name = None
        pending_pax = {}
        global_pax = {}
        archive_size = max(os.path.getsize(path), 1)
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
                    getattr(tarfile, "SOLARIS_XHDTYPE", b"X"),
                ):
                    metadata_records += 1
                    metadata_size += size
                    if metadata_records > cls.MAX_TAR_METADATA_RECORDS:
                        raise ValueError("tar 扩展元数据记录过多，已拒绝解压")
                    if size > cls.MAX_TAR_METADATA_BYTES:
                        raise ValueError(
                            f"tar 扩展元数据过大，已拒绝解压: {member.name} ({size} 字节)"
                        )
                    if metadata_size > cls.MAX_TAR_METADATA_BYTES:
                        raise ValueError(
                            "tar 扩展元数据累计过大，已拒绝解压: "
                            f"{metadata_size} > {cls.MAX_TAR_METADATA_BYTES} 字节"
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
                        if len(global_pax) + len(pending_pax) > cls.MAX_TAR_PAX_FIELDS:
                            raise ValueError("tar PAX 字段过多，已拒绝解压")
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
                if size < 0:
                    raise ValueError(f"tar PAX size 无效: {name}")
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
                    if total_size > archive_size * cls.MAX_COMPRESSION_RATIO:
                        raise ValueError(
                            f"压缩包展开比例过高，已拒绝解压: {path} "
                            f"({total_size / archive_size:.0f}:1)"
                        )
                targets.append((name, is_dir))
                if len(targets) > cls.MAX_ARCHIVE_MEMBERS:
                    raise ValueError("压缩包成员过多，已拒绝解压")
                cls._discard_exact(stream, size)
                cls._skip_tar_padding(stream, size)
        cls._validate_archive_targets(dest, targets)

    @staticmethod
    def _read_exact(stream, size: int, allow_eof: bool = False) -> bytes:
        if size < 0:
            raise ValueError("tar 读取长度无效，已拒绝解压")
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
        if size < 0:
            raise ValueError("tar 读取长度无效，已拒绝解压")
        remaining = size
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("tar 数据提前结束，已拒绝解压")
            remaining -= len(chunk)

    @classmethod
    def _preflight_zip(cls, path: str):
        """在 ZipFile 构造前有界检查 EOCD/ZIP64 和实际中央目录记录。"""
        archive_size = os.path.getsize(path)
        if archive_size < 22:
            raise ValueError(f"ZIP 文件过短或已损坏: {path}")
        tail_size = min(archive_size, 22 + 0xFFFF)
        with open(path, "rb") as stream:
            stream.seek(archive_size - tail_size)
            tail = stream.read(tail_size)
            eocd = cls._find_zip_eocd(tail, archive_size - tail_size, archive_size)
            if eocd is None:
                raise ValueError(f"找不到有效 ZIP 中央目录结束记录: {path}")
            eocd_offset, fields = eocd
            (
                _signature,
                disk_number,
                central_disk,
                entries_on_disk,
                entries_total,
                central_size,
                central_offset,
                _comment_size,
            ) = fields
            if disk_number or central_disk or entries_on_disk != entries_total:
                raise ValueError("不支持分卷 ZIP 压缩包")

            zip64_offset = None
            if (
                entries_total == 0xFFFF
                or central_size == 0xFFFFFFFF
                or central_offset == 0xFFFFFFFF
            ):
                (
                    entries_total,
                    central_size,
                    central_offset,
                    zip64_offset,
                ) = cls._read_zip64_eocd(stream, eocd_offset)

            cls._validate_zip_directory_limits(entries_total, central_size)
            central_end = zip64_offset if zip64_offset is not None else eocd_offset
            central_start = central_end - central_size
            if central_start < 0 or central_offset > central_start:
                raise ValueError("ZIP 中央目录位置无效")
            actual_entries = cls._scan_zip_central_directory(
                stream, central_start, central_end
            )
            if actual_entries != entries_total:
                raise ValueError(
                    "ZIP 中央目录成员数与 EOCD 不一致，已拒绝解压"
                )

    @classmethod
    def _find_zip_eocd(cls, tail: bytes, tail_offset: int, archive_size: int):
        search_end = len(tail)
        while True:
            index = tail.rfind(cls._ZIP_EOCD_SIGNATURE, 0, search_end)
            if index < 0:
                return None
            if index + 22 <= len(tail):
                fields = struct.unpack_from("<4s4H2LH", tail, index)
                comment_size = fields[-1]
                absolute = tail_offset + index
                if absolute + 22 + comment_size == archive_size:
                    return absolute, fields
            search_end = index

    @classmethod
    def _read_zip64_eocd(cls, stream, eocd_offset: int):
        locator_offset = eocd_offset - 20
        if locator_offset < 0:
            raise ValueError("ZIP64 定位器缺失")
        stream.seek(locator_offset)
        locator = cls._read_exact_file(stream, 20, "ZIP64 定位器")
        signature, disk_number, zip64_offset, disk_count = struct.unpack(
            "<4sLQL", locator
        )
        if (
            signature != cls._ZIP64_LOCATOR_SIGNATURE
            or disk_number != 0
            or disk_count != 1
            or zip64_offset < 0
            or zip64_offset + 56 > locator_offset
        ):
            raise ValueError("ZIP64 定位器无效或为分卷压缩包")
        stream.seek(zip64_offset)
        fixed = cls._read_exact_file(stream, 56, "ZIP64 中央目录结束记录")
        fields = struct.unpack("<4sQ2H2L4Q", fixed)
        (
            signature,
            record_size,
            _made_by,
            _needed,
            disk_number,
            central_disk,
            entries_on_disk,
            entries_total,
            central_size,
            central_offset,
        ) = fields
        if (
            signature != cls._ZIP64_EOCD_SIGNATURE
            or record_size < 44
            or zip64_offset + 12 + record_size != locator_offset
            or disk_number != 0
            or central_disk != 0
            or entries_on_disk != entries_total
        ):
            raise ValueError("ZIP64 中央目录结束记录无效")
        return entries_total, central_size, central_offset, zip64_offset

    @classmethod
    def _validate_zip_directory_limits(cls, entries: int, central_size: int):
        if entries > cls.MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                f"压缩包成员过多，已拒绝解压: "
                f"{entries} > {cls.MAX_ARCHIVE_MEMBERS}"
            )
        if central_size > cls.MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
            raise ValueError(
                "ZIP 中央目录过大，已拒绝解压: "
                f"{central_size} > {cls.MAX_ZIP_CENTRAL_DIRECTORY_BYTES} 字节"
            )

    @classmethod
    def _scan_zip_central_directory(cls, stream, start: int, end: int) -> int:
        stream.seek(start)
        count = 0
        while stream.tell() < end:
            signature = cls._read_exact_file(stream, 4, "ZIP 中央目录记录")
            if signature == cls._ZIP_CENTRAL_SIGNATURE:
                fixed = cls._read_exact_file(stream, 42, "ZIP 中央目录记录")
                fields = struct.unpack("<6H3L5H2L", fixed)
                filename_size, extra_size, comment_size = fields[9:12]
                variable_size = filename_size + extra_size + comment_size
                if stream.tell() + variable_size > end:
                    raise ValueError("ZIP 中央目录记录越界")
                stream.seek(variable_size, os.SEEK_CUR)
                count += 1
                if count > cls.MAX_ARCHIVE_MEMBERS:
                    raise ValueError("压缩包成员过多，已拒绝解压")
                continue
            if signature == cls._ZIP_DIGITAL_SIGNATURE:
                size_bytes = cls._read_exact_file(stream, 2, "ZIP 数字签名")
                signature_size = struct.unpack("<H", size_bytes)[0]
                if stream.tell() + signature_size > end:
                    raise ValueError("ZIP 数字签名越界")
                stream.seek(signature_size, os.SEEK_CUR)
                continue
            raise ValueError("ZIP 中央目录记录无效")
        if stream.tell() != end:
            raise ValueError("ZIP 中央目录尺寸无效")
        return count

    @staticmethod
    def _read_exact_file(stream, size: int, label: str) -> bytes:
        data = stream.read(size)
        if len(data) != size:
            raise ValueError(f"{label}提前结束")
        return data

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
        return total_size

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

    def get_file_size(self, version: str, file_path: str):
        return self._folder.get_file_size(self._to_folder_ver(version), file_path)

    def get_file_signature(self, version: str, file_path: str):
        return self._folder.get_file_signature(
            self._to_folder_ver(version), file_path
        )

    def export_file_to_path(self, version: str, file_path: str, target_path: str):
        return self._folder.export_file_to_path(
            self._to_folder_ver(version), file_path, target_path
        )

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
        for d in (
            getattr(self, "_tmp_old", ""),
            getattr(self, "_tmp_new", ""),
            getattr(self, "_tmp_archive_sources", ""),
        ):
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
