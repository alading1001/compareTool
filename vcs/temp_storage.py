import os
import json
import shutil
import sys
import tempfile
import time
from typing import List

from logger import warn


TEMP_DIR_ENV = "COMPARETOOL_TEMP_DIR"
_RUNTIME_TEMP_PARTS = (".tmp", "comparetool_runtime")
_D_DRIVE_TEMP_ROOT = r"D:\applications\_cache\CompareTool\tmp"
_TEMP_MARKER_SUFFIX = ".comparetool_temp_owned"
_TEMP_MAGIC = "CompareTool temp v1"
_STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
_KNOWN_PREFIXES = (
    "cmp_old_", "cmp_new_", "comparetool_folder_old_",
    "comparetool_folder_new_", "comparetool_git_endpoint_",
    "comparetool_svn_endpoint_", "comparetool_git_multi_",
    "comparetool_svn_multi_",
)
_CLEANED_ROOTS = set()
_WARNED_TEMP_FILE_ROOTS = set()


def _runtime_base_dir() -> str:
    """返回源码目录或打包后 exe 所在目录，避开 PyInstaller 的 _MEIPASS。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _system_drive() -> str:
    source = os.environ.get("SystemRoot") or tempfile.gettempdir()
    return os.path.normcase(os.path.splitdrive(os.path.abspath(source))[0])


def _drive(path: str) -> str:
    return os.path.normcase(os.path.splitdrive(os.path.abspath(path))[0])


def _dedupe_paths(paths: List[str]) -> List[str]:
    result = []
    seen = set()
    for path in paths:
        normalized = os.path.abspath(os.path.normpath(path))
        key = os.path.normcase(normalized)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def candidate_temp_roots() -> List[str]:
    """按优先级返回 CompareTool 临时工作根目录候选。"""
    configured = os.environ.get(TEMP_DIR_ENV, "").strip()
    if configured:
        return [os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))]

    runtime_root = os.path.join(_runtime_base_dir(), *_RUNTIME_TEMP_PARTS)
    candidates = []

    if os.name == "nt":
        system_drive = _system_drive()
        # 源码/exe 已位于非系统盘时，优先就近保存，便于定位和清理。
        if _drive(runtime_root) and _drive(runtime_root) != system_drive:
            candidates.append(runtime_root)
        # exe 位于 C 盘时，优先转移到公共 D 盘缓存目录。
        if os.path.isdir("D:\\") and _drive(_D_DRIVE_TEMP_ROOT) != system_drive:
            candidates.append(_D_DRIVE_TEMP_ROOT)
        # 无可用非系统盘时保留可运行回退，不把“必须有 D 盘”写死。
        candidates.append(runtime_root)
    else:
        candidates.append(runtime_root)

    candidates.append(tempfile.gettempdir())
    return _dedupe_paths(candidates)


def create_temp_dir(prefix: str) -> str:
    """创建临时工作目录；仅在更高优先级目录不可用时才回退。"""
    errors = []
    configured = bool(os.environ.get(TEMP_DIR_ENV, "").strip())
    for root in candidate_temp_roots():
        try:
            os.makedirs(root, exist_ok=True)
            _cleanup_stale_temp_dirs(root)
            path = tempfile.mkdtemp(prefix=prefix, dir=root)
            _mark_temp_dir(path)
            if os.name == "nt" and _drive(path) == _system_drive():
                warn(f"CompareTool 临时目录回退到系统盘: {path}")
            return path
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            if configured:
                break

    detail = "\n".join(errors)
    if configured:
        raise RuntimeError(
            f"无法使用环境变量 {TEMP_DIR_ENV} 指定的临时目录。\n{detail}"
        )
    raise RuntimeError(f"无法创建 CompareTool 临时工作目录。\n{detail}")


def open_temp_file(prefix: str = "comparetool_"):
    """在 CompareTool 临时根中打开自动删除的二进制临时文件。"""
    errors = []
    configured = bool(os.environ.get(TEMP_DIR_ENV, "").strip())
    for root in candidate_temp_roots():
        try:
            os.makedirs(root, exist_ok=True)
            stream = tempfile.TemporaryFile(prefix=prefix, dir=root)
            normalized_root = os.path.normcase(os.path.abspath(root))
            if (
                os.name == "nt"
                and _drive(root) == _system_drive()
                and normalized_root not in _WARNED_TEMP_FILE_ROOTS
            ):
                _WARNED_TEMP_FILE_ROOTS.add(normalized_root)
                warn(f"CompareTool 临时文件回退到系统盘: {root}")
            return stream
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            if configured:
                break
    detail = "\n".join(errors)
    if configured:
        raise RuntimeError(
            f"无法使用环境变量 {TEMP_DIR_ENV} 指定的临时目录。\n{detail}"
        )
    raise RuntimeError(f"无法创建 CompareTool 临时文件。\n{detail}")


def remove_temp_dir(path: str):
    if not path:
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
    if not os.path.lexists(path):
        try:
            os.remove(path + _TEMP_MARKER_SUFFIX)
        except FileNotFoundError:
            pass


def _mark_temp_dir(path: str):
    marker = path + _TEMP_MARKER_SUFFIX
    payload = {
        "magic": _TEMP_MAGIC,
        "pid": os.getpid(),
        "created": time.time(),
    }
    try:
        with open(marker, "x", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise


def _cleanup_stale_temp_dirs(root: str):
    key = os.path.normcase(os.path.abspath(root))
    if key in _CLEANED_ROOTS:
        return
    _CLEANED_ROOTS.add(key)
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or not entry.name.startswith(_KNOWN_PREFIXES):
            continue
        marker = entry.path + _TEMP_MARKER_SUFFIX
        try:
            with open(marker, encoding="utf-8") as stream:
                payload = json.load(stream)
            created = float(payload.get("created", 0))
            pid = int(payload.get("pid", 0))
            if payload.get("magic") != _TEMP_MAGIC or now - created < _STALE_AFTER_SECONDS:
                continue
            if _pid_is_alive(pid):
                continue
            remove_temp_dir(entry.path)
            warn(f"已清理 CompareTool 遗留临时目录: {entry.path}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
