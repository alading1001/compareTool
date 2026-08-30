import os
import json
import shutil
import sys
import tempfile
import time
from typing import List

from logger import warn
from stage_ownership import pid_is_alive


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
    runtime_root = os.path.join(_runtime_base_dir(), *_RUNTIME_TEMP_PARTS)
    candidates = []

    if configured:
        # 显式配置仍保持最高优先级；需要避开输入树的调用方可以跳过这个
        # 候选并继续使用后面的安全回退，而普通调用仍会首先使用它。
        candidates.append(
            os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
        )

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


def _path_is_within(path: str, root: str) -> bool:
    path_real = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    root_real = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    try:
        return os.path.normcase(os.path.commonpath([path_real, root_real])) == root_real
    except ValueError:
        return False


def _unsafe_temp_root(root: str, avoid_paths) -> str:
    for avoided in avoid_paths:
        if avoided and _path_is_within(root, avoided):
            return os.path.realpath(os.path.abspath(avoided))
    return ""


def create_temp_dir(
        prefix: str, avoid_paths=None, required_free_bytes: int = 0) -> str:
    """创建临时工作目录；不可写或空间不足时继续尝试下一候选。"""
    errors = []
    avoid_paths = tuple(avoid_paths or ())
    required_free_bytes = max(0, int(required_free_bytes or 0))
    configured_value = os.environ.get(TEMP_DIR_ENV, "").strip()
    configured_root = ""
    if configured_value:
        configured_root = os.path.normcase(os.path.abspath(
            os.path.expandvars(os.path.expanduser(configured_value))
        ))
    for root in candidate_temp_roots():
        unsafe_input = _unsafe_temp_root(root, avoid_paths)
        if unsafe_input:
            errors.append(
                f"{root}: 候选临时根位于比对输入目录内 ({unsafe_input})"
            )
            continue
        try:
            os.makedirs(root, exist_ok=True)
            _cleanup_stale_temp_dirs(root)
            if required_free_bytes:
                free_bytes = shutil.disk_usage(root).free
                if free_bytes < required_free_bytes:
                    errors.append(
                        f"{root}: 可用空间不足，需要 {required_free_bytes} 字节，"
                        f"当前 {free_bytes} 字节"
                    )
                    if (
                        configured_root
                        and os.path.normcase(os.path.abspath(root)) == configured_root
                    ):
                        break
                    continue
            path = tempfile.mkdtemp(prefix=prefix, dir=root)
            unsafe_input = _unsafe_temp_root(path, avoid_paths)
            if unsafe_input:
                shutil.rmtree(path, ignore_errors=True)
                errors.append(
                    f"{root}: 创建出的临时目录位于比对输入目录内 "
                    f"({unsafe_input})"
                )
                continue
            _mark_temp_dir(path)
            if os.name == "nt" and _drive(path) == _system_drive():
                warn(f"CompareTool 临时目录回退到系统盘: {path}")
            return path
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            if (
                configured_root
                and os.path.normcase(os.path.abspath(root)) == configured_root
            ):
                break

    detail = "\n".join(errors)
    if configured_root and not avoid_paths:
        raise RuntimeError(
            f"无法使用环境变量 {TEMP_DIR_ENV} 指定的临时目录。\n{detail}"
        )
    if avoid_paths:
        raise RuntimeError(
            "无法在比对输入目录之外创建 CompareTool 临时工作目录；"
            "所有候选均不安全或不可用。\n"
            f"{detail}"
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
            if pid_is_alive(pid):
                continue
            remove_temp_dir(entry.path)
            warn(f"已清理 CompareTool 遗留临时目录: {entry.path}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
