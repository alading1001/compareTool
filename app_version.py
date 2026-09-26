"""Release identity; packaged metadata is generated, never guessed at runtime."""
import json
import sys
from pathlib import Path

APP_VERSION = "2026.09.26"


def build_info():
    fallback = {"version": APP_VERSION, "build_id": "source",
                "built_at": "未打包", "commit": "", "dirty": True}
    if not getattr(sys, "frozen", False):
        return fallback
    try:
        path = Path(__file__).resolve().parent / "build_info.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != APP_VERSION:
            raise ValueError("invalid build metadata")
        if not all(isinstance(data.get(k), str) for k in
                   ("build_id", "built_at", "commit", "source_sha256")):
            raise ValueError("incomplete build metadata")
        return data
    except (OSError, ValueError):
        return dict(fallback, build_id="unknown", built_at="构建信息缺失")


def window_title():
    data = build_info()
    return f"代码比对报告工具 v{APP_VERSION} [{data['build_id']}]"
