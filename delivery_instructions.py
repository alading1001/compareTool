import os
import tempfile

from vcs.base import ChangeType


DELIVERY_INSTRUCTIONS_FILENAME = "上线操作说明.txt"


def prepare_delivery_instructions(project_results: list, target_path: str):
    """在目标同目录生成带 BOM 的暂存说明文件，返回 (stage, target)。"""
    target_path = os.path.abspath(target_path)
    parent = os.path.dirname(target_path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, stage_path = tempfile.mkstemp(
        prefix=".comparetool_delivery_",
        suffix=".txt",
        dir=parent,
    )
    os.close(fd)
    try:
        write_delivery_instructions(project_results, stage_path)
        return stage_path, target_path
    except BaseException:
        if os.path.isfile(stage_path):
            os.remove(stage_path)
        raise


def write_delivery_instructions(project_results: list, output_path: str):
    delete_entries = []
    rename_entries = []
    deleted_count = 0

    for project in project_results:
        project_name = str(project.get("project_name", "") or "").strip()
        result = project["diff_result"]
        for file_diff in result.files:
            if file_diff.change_type == ChangeType.DELETED:
                deleted_count += 1
                delete_entries.append((
                    _delivery_path(project_name, file_diff.file_path),
                    "删除文件",
                ))
            elif file_diff.change_type == ChangeType.RENAMED:
                old_path = file_diff.old_path or file_diff.file_path
                old_delivery_path = _delivery_path(project_name, old_path)
                new_delivery_path = _delivery_path(project_name, file_diff.file_path)
                delete_entries.append((old_delivery_path, "重命名旧路径"))
                rename_entries.append((old_delivery_path, new_delivery_path))

    delete_entries.sort(key=lambda item: item[0].casefold())
    rename_entries.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))

    lines = [
        "CompareTool 上线操作说明",
        "========================",
        "",
        "newVersion 中保存的是本次需要提交的完整新文件。",
        "仅复制 newVersion 不会自动删除目标环境中的旧文件，请按下面清单处理。",
        "",
        f"删除文件：{deleted_count} 个",
        f"重命名文件：{len(rename_entries)} 个",
        f"需要删除的旧路径合计：{len(delete_entries)} 个",
        "",
        "一、需要从目标环境删除的旧路径",
        "================================",
    ]
    if delete_entries:
        lines.extend(f"[{reason}] {path}" for path, reason in delete_entries)
    else:
        lines.append("本次无需要删除或重命名的旧文件。")

    lines.extend([
        "",
        "二、重命名对应关系",
        "==================",
    ])
    if rename_entries:
        for old_path, new_path in rename_entries:
            lines.append(f"{old_path}")
            lines.append(f"  -> {new_path}")
    else:
        lines.append("本次无重命名文件。")

    lines.extend([
        "",
        "说明：新增和修改文件不在此重复列出，请直接以 newVersion 目录为准。",
        "",
    ])
    with open(output_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
        handle.write("\n".join(lines))


def _delivery_path(project_name: str, file_path: str) -> str:
    normalized = (file_path or "").replace("\\", "/").lstrip("/")
    return f"{project_name}/{normalized}" if project_name else normalized
