"""
简易文件日志模块，用于调试 SVN/Git 命令执行过程。
日志写入 compareTool.log，与 exe/脚本同目录。
"""
import os
import sys
import datetime
import threading

# 日志文件路径：与配置 JSON 同目录
if getattr(sys, 'frozen', False):
    _LOG_DIR = os.path.dirname(sys.executable)
else:
    _LOG_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(_LOG_DIR, "compareTool.log")
_MAX_SIZE = 512 * 1024  # 512KB 后轮转
_MAX_ENTRY_SIZE = 64 * 1024
_write_lock = threading.Lock()


def _write(level: str, msg: str):
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = f"[{ts}] [{level}] {msg}\n".encode("utf-8", errors="replace")
        if len(payload) > _MAX_ENTRY_SIZE:
            note = "\n[日志过长，已截断]\n".encode("utf-8")
            payload = payload[:_MAX_ENTRY_SIZE - len(note)].decode("utf-8", errors="ignore").encode("utf-8") + note
        with _write_lock:
            # 在写入前轮转，单条超长报错也不能突破单文件限制。
            if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) + len(payload) > _MAX_SIZE:
                backup = LOG_FILE + ".bak"
                os.replace(LOG_FILE, backup)
                if os.path.getsize(backup) > _MAX_SIZE:
                    # 兼容旧版可能产生的超大单条日志，只保留末尾的诊断内容。
                    with open(backup, "rb") as stream:
                        stream.seek(-_MAX_SIZE, os.SEEK_END)
                        tail = stream.read().decode("utf-8", errors="ignore").encode("utf-8")
                    with open(backup, "wb") as stream:
                        stream.write(tail)
            with open(LOG_FILE, "ab") as f:
                f.write(payload)
    except Exception:
        pass  # 日志写入失败不抛异常


def info(msg: str):
    pass  # 正常流程不写日志，仅在出错时记录


def warn(msg: str):
    _write("WARN", msg)


def error(msg: str):
    _write("ERROR", msg)


def cmd(args: list, returncode: int, stdout: str = "", stderr: str = ""):
    """记录一条命令执行"""
    _write("CMD", f"cmd={' '.join(args)} rc={returncode}")
    if stdout:
        _write("CMD", f"stdout: {stdout[:500]}")
    if stderr:
        _write("CMD", f"stderr: {stderr[:500]}")
