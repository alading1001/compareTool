import ntpath
import os
import re
import stat


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
