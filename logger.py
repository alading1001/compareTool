"""
简易文件日志模块，用于调试 SVN/Git 命令执行过程。
日志写入 compareTool.log，与 exe/脚本同目录。
"""
import os
import sys
import datetime

# 日志文件路径：与配置 JSON 同目录
if getattr(sys, 'frozen', False):
    _LOG_DIR = os.path.dirname(sys.executable)
else:
    _LOG_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(_LOG_DIR, "compareTool.log")
_MAX_SIZE = 512 * 1024  # 512KB 后轮转


def _write(level: str, msg: str):
    try:
        # 超过大小限制则轮转
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > _MAX_SIZE:
            bak = LOG_FILE + ".bak"
            if os.path.isfile(bak):
                os.remove(bak)
            os.rename(LOG_FILE, bak)

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{level}] {msg}\n")
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
