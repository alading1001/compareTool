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
    if stem in WINDOWS_RESERVED_NAMES or WINDOWS_SHORT_ALIAS_RE.fullmatch(cleaned):
        cleaned = "_" + cleaned
    return cleaned


def split_safe_relative_path(path: str, label: str = "路径"):
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
        if WINDOWS_SHORT_ALIAS_RE.fullmatch(part):
            raise ValueError(
                f"{label}疑似 Windows 8.3 短名称，可能覆盖同目录长文件名: {path}"
            )
    return parts


def safe_join(base_dir: str, rel_path: str, label: str = "路径") -> str:
    parts = split_safe_relative_path(rel_path, label=label)
    root = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(root, *parts))
    try:
        inside_root = os.path.commonpath([root, target]) == root
    except ValueError:
        inside_root = False
    if not inside_root:
        raise ValueError(f"{label}越界: {rel_path}")
    return target


def is_link_or_junction(path: str) -> bool:
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
    return reparse_tag in {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
    }


def ensure_no_link_components(root: str, path: str, label: str = "路径") -> None:
    """确认 root 到 path 的所有现存组件都不是链接、联接点或挂载点。"""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    try:
        inside = os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(f"{label}越出事务根: {path}")

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
            raise RuntimeError("已打开对象是重解析点")
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
def open_regular_file_no_links(path: str):
    """不跟随最终重解析点地打开普通文件，并返回可读取的二进制流。"""
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
        handle = create_file(
            path,
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,  # SHARE R/W/DELETE
            None,
            3,  # OPEN_EXISTING
            0x200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
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

    raw_stream = os.fdopen(fd, "rb", closefd=True)
    stream = _NamedBinaryReader(raw_stream, path)
    try:
        regular_file_handle_identity(stream)
        yield stream
    finally:
        raw_stream.close()


def regular_file_path_identity(path: str) -> tuple:
    """通过安全句柄取得路径当前指向的普通文件身份。"""
    with open_regular_file_no_links(path) as stream:
        return regular_file_handle_identity(stream)
