import ntpath
import os
import re
import stat
from contextlib import contextmanager


WINDOWS_INVALID_CHARS = set('<>:"|?*')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    "COM¹", "COM²", "COM³",
    "LPT¹", "LPT²", "LPT³",
}
WINDOWS_SHORT_ALIAS_RE = re.compile(
    r"^[^ .]{1,6}~[0-9]+(?:\.[^ .]{0,3})?$",
    re.IGNORECASE,
)
WINDOWS_REPARSE_NAME_SURROGATE = 0x20000000


def windows_path_key(value: str) -> str:
    """返回 Windows 路径等价键，不做会扩展字符的 Unicode casefold。"""
    return ntpath.normcase(os.fspath(value))


def windows_directories_replaced_by_files(directories, file_paths):
    """近线性找出被同名文件替换的旧目录前缀，并保留旧端拼写。"""
    file_keys = {
        windows_path_key(normalized)
        for path in file_paths
        if (normalized := path.replace("\\", "/").strip("/"))
    }
    replaced = set()
    for directory in directories:
        normalized = directory.replace("\\", "/").strip("/")
        if not normalized:
            continue
        prefix = []
        for part in normalized.split("/"):
            prefix.append(part)
            old_prefix = "/".join(prefix)
            if windows_path_key(old_prefix) in file_keys:
                replaced.add(old_prefix)
                break
    return sorted(replaced)


def is_name_surrogate_reparse_tag(tag: int) -> bool:
    """Windows name-surrogate tag 会把路径解析到另一个命名空间对象。"""
    return bool(int(tag or 0) & WINDOWS_REPARSE_NAME_SURROGATE)


def sanitize_windows_component(value: str) -> str:
    """清洗一个可安全作为 Windows 单级目录/文件名的用户输入。"""
    raw = (value or "").strip()
    cleaned = "".join(
        "_" if ord(char) < 32 or char in WINDOWS_INVALID_CHARS or char in "/\\" else char
        for char in raw
    ).strip(" .")
    if not cleaned or not cleaned.strip("._"):
        return ""
    stem = cleaned.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned


def split_safe_relative_path(
        path: str, label: str = "路径", *, reject_short_alias: bool = False):
    """解析可安全落到 Windows 文件系统的相对路径。"""
    raw = (path or "").replace("\\", "/")
    drive, _ = ntpath.splitdrive(raw)
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if (
        not raw or
        not parts or
        "\x00" in raw or
        drive or
        raw.startswith("/") or
        any(part == ".." for part in parts)
    ):
        raise ValueError(f"{label}不安全: {path}")

    for part in parts:
        if any(ord(char) < 32 or char in WINDOWS_INVALID_CHARS for char in part):
            raise ValueError(f"{label}包含 Windows 非法字符: {path}")
        if part.endswith((" ", ".")):
            raise ValueError(f"{label}包含 Windows 不可保真的尾随空格或点: {path}")
        stem = part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"{label}包含 Windows 保留名称: {path}")
        if reject_short_alias and WINDOWS_SHORT_ALIAS_RE.fullmatch(part):
            raise ValueError(
                f"{label}疑似 Windows 8.3 短名称，可能覆盖同目录长文件名: {path}"
            )
    return parts


def safe_join(
        base_dir: str, rel_path: str, label: str = "路径", *,
        reject_short_alias: bool = False) -> str:
    parts = split_safe_relative_path(
        rel_path, label=label, reject_short_alias=reject_short_alias
    )
    root = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(root, *parts))
    try:
        inside_root = os.path.commonpath([root, target]) == root
    except ValueError:
        inside_root = False
    if not inside_root:
        raise ValueError(f"{label}越界: {rel_path}")
    return target


def ensure_tree_directory(root: str, directory: str, cache=None, label="输出") -> str:
    """在专用暂存树逐级创建目录，拒绝真实短别名而非所有带 ~ 的名字。"""
    root = os.path.abspath(root)
    directory = os.path.abspath(directory)
    ensure_no_link_components(root, directory, label=f"{label}目录")
    relative = os.path.relpath(directory, root)
    if relative == ".":
        return root
    cache = cache if cache is not None else {}
    current = root
    for component in relative.split(os.sep):
        known = _tree_directory_entries(current, cache, label)
        key = windows_path_key(component)
        desired = os.path.join(current, component)
        existing = known.get(key)
        if existing is not None:
            if is_link_or_junction(existing) or not os.path.isdir(existing):
                raise ValueError(f"{label}目录与现有文件或链接冲突: {desired}")
            current = existing
            continue
        if os.path.lexists(desired):
            raise ValueError(f"{label}目录与现有 Windows 别名冲突: {desired}")
        try:
            os.mkdir(desired)
        except FileExistsError as exc:
            raise ValueError(f"{label}目录发生 Windows 名称碰撞: {desired}") from exc
        if is_link_or_junction(desired) or not os.path.isdir(desired):
            raise ValueError(f"{label}目录创建后身份异常: {desired}")
        known[key] = desired
        current = desired
    ensure_no_link_components(root, current, label=f"{label}目录")
    return current


def _tree_directory_entries(directory: str, cache: dict, label: str) -> dict:
    key = os.path.abspath(directory)
    if key not in cache:
        known = {}
        with os.scandir(directory) as entries:
            for entry in entries:
                name_key = windows_path_key(entry.name)
                if name_key in known:
                    raise ValueError(f"{label}目录存在 Windows 名称碰撞: {entry.path}")
                known[name_key] = entry.path
        cache[key] = known
    return cache[key]


def open_new_tree_file(root: str, target: str, cache=None, label="输出"):
    """排他创建新文件；调用方持有返回句柄，或关闭后交给路径型 writer。"""
    cache = cache if cache is not None else {}
    parent = ensure_tree_directory(root, os.path.dirname(target), cache, label)
    target = os.path.join(parent, os.path.basename(target))
    known = _tree_directory_entries(parent, cache, label)
    key = windows_path_key(os.path.basename(target))
    if key in known:
        raise ValueError(f"{label}文件与现有 Windows 路径或短名称别名冲突: {target}")
    try:
        stream = open(target, "xb")
    except FileExistsError as exc:
        raise ValueError(
            f"{label}文件与现有 Windows 路径或短名称别名冲突: {target}"
        ) from exc
    known[key] = target
    return target, stream


def metadata_is_link_or_junction(metadata) -> bool:
    """Classify links from an existing stat result without another path query."""
    if stat.S_ISLNK(metadata.st_mode):
        return True
    if os.name != "nt":
        return False
    return is_name_surrogate_reparse_tag(
        int(getattr(metadata, "st_reparse_tag", 0) or 0)
    )


def is_link_or_junction(path: str) -> bool:
    # On current Windows Python, one lstat carries both symlink mode and
    # st_reparse_tag. Return from that metadata instead of asking the OS
    # separately via islink(), isjunction(), and lstat().
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    if os.name != "nt" or hasattr(metadata, "st_reparse_tag"):
        return metadata_is_link_or_junction(metadata)
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction and is_junction(path):
        return True
    if os.name != "nt":
        return False
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    reparse_tag = getattr(metadata, "st_reparse_tag", 0)
    return is_name_surrogate_reparse_tag(reparse_tag)


def ensure_no_link_components(root: str, path: str, label: str = "路径") -> None:
    """确认 root 到 path 的所有现存组件都不是链接、联接点或挂载点。"""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    try:
        inside = os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"{label}越出指定根目录: {path}")

    current = root
    components = [] if os.path.normcase(path) == os.path.normcase(root) else os.path.relpath(
        path, root
    ).split(os.sep)
    for component in [""] + components:
        if component:
            current = os.path.join(current, component)
        if os.path.lexists(current) and is_link_or_junction(current):
            raise ValueError(f"{label}祖先包含符号链接或联接点: {current}")


def _nanoseconds(metadata, name: str) -> int:
    value = getattr(metadata, name, None)
    if value is not None:
        return int(value)
    return int(getattr(metadata, name.removesuffix("_ns")) * 1_000_000_000)


def regular_file_handle_identity(stream) -> tuple:
    """返回已打开普通文件的稳定身份和竞态检测元数据。"""
    metadata = os.fstat(stream.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("已打开对象不是普通文件")

    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        info = BY_HANDLE_FILE_INFORMATION()
        handle = msvcrt.get_osfhandle(stream.fileno())
        get_info = ctypes.windll.kernel32.GetFileInformationByHandle
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, ctypes.byref(info)):
            raise ctypes.WinError()
        if info.dwFileAttributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            reparse_tag = int(getattr(metadata, "st_reparse_tag", 0) or 0)
            if not reparse_tag:
                class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
                    _fields_ = [
                        ("FileAttributes", wintypes.DWORD),
                        ("ReparseTag", wintypes.DWORD),
                    ]

                tag_info = FILE_ATTRIBUTE_TAG_INFO()
                get_info_ex = ctypes.windll.kernel32.GetFileInformationByHandleEx
                get_info_ex.argtypes = [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                ]
                get_info_ex.restype = wintypes.BOOL
                if not get_info_ex(
                    handle,
                    9,  # FileAttributeTagInfo
                    ctypes.byref(tag_info),
                    ctypes.sizeof(tag_info),
                ):
                    raise ctypes.WinError()
                reparse_tag = int(tag_info.ReparseTag)
            if is_name_surrogate_reparse_tag(reparse_tag):
                raise RuntimeError("已打开对象是路径重定向重解析点")
        stable_id = (
            "windows",
            int(info.dwVolumeSerialNumber),
            (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow),
        )
    else:
        stable_id = (
            "posix",
            int(getattr(metadata, "st_dev", 0)),
            int(getattr(metadata, "st_ino", 0)),
        )
    return (
        stable_id,
        int(metadata.st_size),
        _nanoseconds(metadata, "st_mtime_ns"),
        _nanoseconds(metadata, "st_ctime_ns"),
    )


class _NamedBinaryReader:
    """保留真实路径名，同时代理安全打开的底层二进制流。"""

    def __init__(self, stream, path: str):
        self._stream = stream
        self.name = path

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextmanager
def open_regular_file_no_links(path: str, *, deny_writes: bool = False):
    """不跟随最终重解析点地打开普通文件，并返回可读取的二进制流。

    ``deny_writes`` 用于必须在整个任务期间保持同一内容的输入文件。Windows
    只共享读取权限，从内核层拒绝其它句柄写入或删除；POSIX 使用共享 advisory
    lock，并仍由调用方在结束时复核身份与内容摘要。
    """
    path = os.path.abspath(path)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        share_mode = 0x00000001 if deny_writes else (
            0x00000001 | 0x00000002 | 0x00000004
        )
        # Always open the final path component itself. Normal files are
        # unaffected, while a symlink/junction cannot be followed in the
        # interval between a path pre-check and CreateFileW.
        open_flags = 0x08000000 | 0x200000  # SEQUENTIAL_SCAN | OPEN_REPARSE_POINT
        handle = create_file(
            path,
            0x80000000,  # GENERIC_READ
            share_mode,
            None,
            3,  # OPEN_EXISTING
            open_flags,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError()
        try:
            fd = msvcrt.open_osfhandle(
                int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            ctypes.windll.kernel32.CloseHandle(handle)
            raise
    else:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        if deny_writes:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_SH)
            except BaseException:
                os.close(fd)
                raise

    raw_stream = os.fdopen(fd, "rb", closefd=True)
    stream = _NamedBinaryReader(raw_stream, path)
    try:
        stream._initial_identity = regular_file_handle_identity(stream)
        if is_link_or_junction(path):
            raise RuntimeError("已打开文件路径被替换成符号链接或联接点")
        yield stream
    finally:
        raw_stream.close()


def regular_file_path_identity(path: str) -> tuple:
    """通过安全句柄取得路径当前指向的普通文件身份。"""
    with open_regular_file_no_links(path) as stream:
        return stream._initial_identity
