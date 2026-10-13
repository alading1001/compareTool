"""Release identity; packaged metadata is generated, never guessed at runtime."""
import json
import re
import sys
from datetime import date
from pathlib import Path

APP_VERSION = "开发版"


def is_release_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]*", value):
        return False
    try:
        date.fromisoformat(value[:10].replace(".", "-"))
        return True
    except ValueError:
        return False


def build_info():
    fallback = {"version": APP_VERSION, "build_id": "source",
                "built_at": "未打包", "commit": "", "dirty": True}
    if not getattr(sys, "frozen", False):
        return fallback
    try:
        path = Path(__file__).resolve().parent / "build_info.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not is_release_version(data.get("version")):
            raise ValueError("invalid build metadata")
        if not all(isinstance(data.get(k), str) for k in
                   ("build_id", "built_at", "commit", "source_sha256")):
            raise ValueError("incomplete build metadata")
        return data
    except (OSError, ValueError):
        return dict(fallback, version="unknown", build_id="unknown", built_at="构建信息缺失")


def window_title():
    data = build_info()
    version = f"v{data['version']}" if is_release_version(data['version']) else data['version']
    return f"代码比对报告工具 {version} [{data['build_id']}]"
