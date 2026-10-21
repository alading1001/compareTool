"""按内容探测容器；探测不代替解包时的完整结构与正文校验。"""
import os
import tarfile
import zipfile

from path_safety import open_regular_file_no_links


ARCHIVE_SUFFIXES = ('.zip', '.jar', '.war', '.ear', '.aar', '.tar',
                    '.gz', '.tgz', '.bz2', '.tbz2', '.7z', '.rar', '.xz')
SUPPORTED_FORMATS = {'zip', 'tar', 'gzip', 'bzip2'}


def is_archive_name(path):
    return str(path or '').lower().endswith(ARCHIVE_SUFFIXES)


def is_tar_header(header):
    if len(header) != 512:
        return False
    if header == b'\0' * 512:
        return True
    # 损坏的 ustar 仍交给 TAR 校验器报错，不能回退成普通文本。
    if header[257:263] in (b'ustar\0', b'ustar '):
        return True
    try:
        tarfile.TarInfo.frombuf(header, tarfile.ENCODING, 'surrogateescape')
        return True
    except (tarfile.TarError, ValueError):
        return False


def detect_archive_stream(source):
    position = source.tell()
    try:
        source.seek(0)
        header = source.read(512)
        if header.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08', b'PK\x06\x06')):
            return 'zip'
        if header.startswith(b'\x1f\x8b'):
            return 'gzip'
        if header.startswith(b'BZh'):
            return 'bzip2'
        if header.startswith(b'7z\xbc\xaf\x27\x1c'):
            return '7z'
        if header.startswith(b'Rar!\x1a\x07'):
            return 'rar'
        if header.startswith(b'\xfd7zXZ\x00'):
            return 'xz'
        if is_tar_header(header):
            return 'tar'
        # 保留带自解压前缀的 ZIP；标准库只在文件尾部做有界探测。
        source.seek(0)
        if zipfile.is_zipfile(source):
            return 'zip'
        return None
    finally:
        source.seek(position)


def detect_archive_path(path):
    with open_regular_file_no_links(os.fspath(path), deny_writes=True) as source:
        return detect_archive_stream(source)
