"""排除规则的来源、读取与运行快照；不改变现有 glob 匹配语义。"""
from dataclasses import dataclass
import hashlib
import os
import re

from vcs.base import BaseVCS


ENCODINGS = ("utf-8-sig", "gb18030")


@dataclass(frozen=True)
class RuleSnapshot:
    text: str
    kind: str = "manual"
    path: str = ""
    encoding: str = "utf-8"
    sha256: str = ""

    @property
    def rules(self):
        # 注释仅属于新增的外部文件格式；旧手工规则中的 # 仍是字面字符。
        return tuple((number, line.strip())
                     for number, line in enumerate(self.text.splitlines(), 1)
                     if line.strip() and not (self.kind == "file" and line.lstrip().startswith("#")))

    @property
    def patterns(self):
        return tuple(pattern for _number, pattern in self.rules)

    @property
    def effective_text(self):
        return "\n".join(self.patterns)

    def to_dict(self):
        return dict(text=self.text, kind=self.kind, path=self.path,
                    encoding=self.encoding, sha256=self.sha256)

    def matches(self, path):
        path = path.replace("\\", "/")
        if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path) or ".." in path.split("/"):
            raise ValueError("请输入相对比较根的文件路径，不带盘符、开头 / 或 ..")
        return tuple((number, pattern) for number, pattern in self.rules
                     if BaseVCS._match_glob_pattern(path, pattern, implicit_any_depth=True))


def manual_snapshot(text):
    if not isinstance(text, str):
        raise ValueError("排除规则必须是文本")
    return RuleSnapshot(text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def normalize_source(value, base_dir=""):
    if not isinstance(value, dict) or value.get("kind") not in ("manual", "file"):
        raise ValueError("排除规则来源无效，请重新选择手工规则或外部文件")
    if value["kind"] == "manual":
        return {"kind": "manual"}
    path = value.get("path")
    encoding = value.get("encoding", "utf-8-sig")
    follow = value.get("follow", True)
    if not isinstance(path, str) or not path or encoding not in ENCODINGS or not isinstance(follow, bool):
        raise ValueError("外部排除规则的路径、编码或跟随设置无效")
    path = os.path.abspath(os.path.join(base_dir, path))
    return dict(kind="file", path=path, encoding=encoding, follow=follow)


def read_rule_file(source):
    source = normalize_source(source)
    if source["kind"] != "file":
        raise ValueError("尚未关联排除规则文件")
    path = source["path"]
    try:
        with open(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            content = stream.read()
            after = os.fstat(stream.fileno())
        current = os.stat(path)
        # Windows 上 Python 的句柄 fstat 与路径 stat 对 ctime 的解释可能不同；
        # 用两者一致的文件 ID、长度和 mtime 比较，不把正常改写误报为并发变化。
        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current) or len(content) != after.st_size:
            raise ValueError("读取期间文件发生变化，请保存文件后重试")
        text = content.decode(source["encoding"], errors="strict")
        if "\x00" in text:
            raise ValueError("文件包含 NUL，不能作为排除规则文本")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"无法读取排除规则文件：{path}\n编码：{source['encoding']}\n{exc}") from exc
    return RuleSnapshot(text, "file", path, source["encoding"], hashlib.sha256(content).hexdigest())


def restore_snapshot(value):
    if not isinstance(value, dict):
        raise ValueError("排除规则快照缺失或损坏")
    if not all(isinstance(value.get(key), str) for key in ("text", "kind", "path", "encoding", "sha256")):
        raise ValueError("排除规则快照字段无效")
    if value["kind"] not in ("manual", "file") or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ValueError("排除规则快照来源或摘要无效")
    if value["kind"] == "file":
        normalize_source(dict(kind="file", path=value["path"], encoding=value["encoding"]))
    return RuleSnapshot(**{key: value[key] for key in ("text", "kind", "path", "encoding", "sha256")})


def freeze_tasks(tasks, *, best_effort=False):
    """整批在任何项目开始前解析一次；相同文件及编码只读一次，不修改保存任务。"""
    files = {}
    frozen = []
    for original in tasks:
        task = dict(original)
        if task.get("_exclude_frozen"):
            # 仅工作线程内部交接使用；配置恢复不保留此标志。
            frozen.append(task)
            continue
        try:
            source = normalize_source(task.get("exclude_source", {"kind": "manual"}))
            if source["kind"] == "file" and source["follow"]:
                key = (source["path"], source["encoding"])
                if key not in files:
                    try:
                        files[key] = read_rule_file(source)
                    except ValueError as exc:
                        files[key] = exc
                if isinstance(files[key], ValueError):
                    raise files[key]
                snapshot = files[key]
            elif source["kind"] == "file":
                snapshot = restore_snapshot(task.get("exclude_snapshot"))
                if (snapshot.kind, snapshot.path, snapshot.encoding) != ("file", source["path"], source["encoding"]):
                    raise ValueError("固定快照与关联文件来源不一致，请重新关联")
            else:
                snapshot = manual_snapshot(task.get("exclude_rules", ""))
        except ValueError as exc:
            message = f"项目 {task.get('project_name', '')} 的排除规则无效：\n{exc}"
            if not best_effort:
                raise ValueError(message) from exc
            # 不创建 VCS，也不拿旧预览/空规则继续比较该项目。
            task.update(_exclude_error=message, exclude_snapshot={}, _exclude_frozen=True)
            frozen.append(task)
            continue
        task.update(exclude_rules=snapshot.effective_text, exclude_snapshot=snapshot.to_dict(),
                    exclude_source=source, _exclude_frozen=True)
        frozen.append(task)
    return frozen
