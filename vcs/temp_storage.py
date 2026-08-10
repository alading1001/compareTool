import os
import sys
import tempfile
from typing import List

from logger import warn


TEMP_DIR_ENV = "COMPARETOOL_TEMP_DIR"
_RUNTIME_TEMP_PARTS = (".tmp", "comparetool_runtime")
_D_DRIVE_TEMP_ROOT = r"D:\applications\_cache\CompareTool\tmp"


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
            path = tempfile.mkdtemp(prefix=prefix, dir=root)
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
