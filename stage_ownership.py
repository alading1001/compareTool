import os
import json
import time
import stat
import threading

from logger import warn


OWNERSHIP_SUFFIX = ".comparetool_owned"
OWNERSHIP_MAGIC = "CompareTool owned staging item v2"
LEGACY_OWNERSHIP_MAGIC = "CompareTool owned staging item v1\n"
LEGACY_ABANDONED_SECONDS = 60 * 60

# 只记录当前进程新建的对象，不从历史 sidecar 恢复删除权限。
_owned_paths = {}
_owned_paths_lock = threading.Lock()


def _path_identity(path):
    metadata = os.lstat(path)
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode),
            getattr(metadata, 'st_reparse_tag', 0))


def record_owned_path(path, *, owner=None):
    path = os.path.abspath(path)
    parents = []
    parent = os.path.dirname(path)
    while True:
        parents.append((parent, _path_identity(parent)))
        ancestor = os.path.dirname(parent)
        if ancestor == parent:
            break
        parent = ancestor
    record = (_path_identity(path), tuple(parents), os.path.abspath(owner or path))
    with _owned_paths_lock:
        _owned_paths[path] = record


def owned_path_owner(path):
    path = os.path.abspath(path)
    with _owned_paths_lock:
        record = _owned_paths.get(path)
    return record[2] if record else path


def owned_path_is_current(path):
    path = os.path.abspath(path)
    with _owned_paths_lock:
        record = _owned_paths.get(path)
    if record is None:
        return False
    try:
        return (_path_identity(path) == record[0]
                and all(_path_identity(parent) == identity for parent, identity in record[1]))
    except OSError:
        return False


def forget_owned_path(path):
    with _owned_paths_lock:
        _owned_paths.pop(os.path.abspath(path), None)


def replace_owned_file(source, target):
    """报告原子写入只接管本次仍有效的目标，显式转移新文件身份。"""
    source, target = os.path.abspath(source), os.path.abspath(target)
    with _owned_paths_lock:
        source_record = _owned_paths.get(source)
        target_record = _owned_paths.get(target)
    if not owned_path_is_current(source) or (target_record and not owned_path_is_current(target)):
        raise RuntimeError('报告暂存对象或父目录已被替换，已停止写入')
    os.replace(source, target)
    with _owned_paths_lock:
        if target_record:
            _owned_paths[target] = (source_record[0], target_record[1], target_record[2])
        _owned_paths.pop(source, None)


def cleanup_owned_path(path, remover):
    """对象或任一父目录变化时保留现场，不凭名称或旧标记删除。"""
    if os.path.lexists(path):
        if not owned_path_is_current(path):
            warn('暂存对象或父目录已被替换或无法确认所有权，保留该路径: ' + str(path))
            forget_owned_path(path)
            forget_owned_path(ownership_marker(path))
            return False
        try:
            remover(path)
        except OSError:
            forget_owned_path(path)
            forget_owned_path(ownership_marker(path))
            raise
    if os.path.lexists(path):
        return False
    forget_owned_path(path)
    remove_ownership_marker(path)
    return True


def ownership_marker(path: str) -> str:
    return os.path.abspath(path) + OWNERSHIP_SUFFIX


def mark_owned(path: str, owner_pid: int = None):
    with _owned_paths_lock:
        recorded = os.path.abspath(path) in _owned_paths
    if recorded:
        if not owned_path_is_current(path):
            raise RuntimeError('暂存对象在登记所有权前已被替换，已停止处理')
    else:
        record_owned_path(path)
    marker = ownership_marker(path)
    payload = {
        "magic": OWNERSHIP_MAGIC,
        "pid": os.getpid() if owner_pid is None else int(owner_pid),
        "created": time.time(),
    }
    with open(marker, "x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    record_owned_path(marker)
    return marker


def is_owned(path: str) -> bool:
    payload = _read_ownership(path)
    return payload is not None


def ownership_is_abandoned(path: str) -> bool:
    payload = _read_ownership(path)
    if payload is None:
        return False
    if payload.get("legacy"):
        try:
            age = time.time() - os.path.getmtime(ownership_marker(path))
        except OSError:
            return False
        return age >= LEGACY_ABANDONED_SECONDS
    return not _pid_is_alive(payload.get("pid", 0))


def _read_ownership(path: str):
    marker = ownership_marker(path)
    try:
        with open(marker, "r", encoding="utf-8") as stream:
            raw = stream.read(4096)
        if raw == LEGACY_OWNERSHIP_MAGIC:
            return {"legacy": True}
        payload = json.loads(raw)
        if (
            isinstance(payload, dict)
            and payload.get("magic") == OWNERSHIP_MAGIC
            and isinstance(payload.get("pid"), int)
        ):
            return payload
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 的 os.kill(pid, 0) 并不等价于 POSIX 的只读存活探测，
        # 某些 Python/Windows 组合会直接报 OSError，把当前活进程误判为
        # 已退出并清掉仍在生成中的 stage。用同步句柄做零超时查询。
        try:
            import ctypes
            from ctypes import wintypes

            synchronize = 0x00100000
            wait_timeout = 0x00000102
            access_denied = 5
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                return ctypes.get_last_error() == access_denied
            try:
                return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            # 无法可靠探测时宁可保留 stage，不能以不确定性授权删除。
            return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def pid_is_alive(pid: int) -> bool:
    """公共的保守进程存活探测；无法确认时按存活处理。"""
    return _pid_is_alive(pid)


def remove_ownership_marker(path: str):
    marker = ownership_marker(path)
    try:
        if owned_path_is_current(marker):
            os.remove(marker)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    finally:
        forget_owned_path(marker)
