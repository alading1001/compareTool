import os
import json
import time


OWNERSHIP_SUFFIX = ".comparetool_owned"
OWNERSHIP_MAGIC = "CompareTool owned staging item v2"
LEGACY_OWNERSHIP_MAGIC = "CompareTool owned staging item v1\n"
LEGACY_ABANDONED_SECONDS = 60 * 60


def ownership_marker(path: str) -> str:
    return os.path.abspath(path) + OWNERSHIP_SUFFIX


def mark_owned(path: str, owner_pid: int = None):
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


def remove_ownership_marker(path: str):
    try:
        os.remove(ownership_marker(path))
    except FileNotFoundError:
        pass
    except OSError:
        pass
