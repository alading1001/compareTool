import os
from file_exporter import FileExporter
from vcs.base import ChangeType


DELIVERY_INSTRUCTIONS_FILENAME = "上线操作说明.txt"


def single_delivery_instructions_filename(project_name: str) -> str:
    """单项目说明按项目隔离，避免同批次后生成项目覆盖前一项目。"""
    name = str(project_name or "").strip()
    return f"{name}_{DELIVERY_INSTRUCTIONS_FILENAME}" if name else DELIVERY_INSTRUCTIONS_FILENAME


def prepare_delivery_instructions(
    project_results: list,
    target_path: str,
    trusted_root: str = "",
):
    """在目标同目录生成带 BOM 的暂存说明文件，返回 (stage, target)。"""
    target_path = os.path.abspath(target_path)
    stage_path = FileExporter._make_stage_file(
        target_path,
        trusted_root or os.path.dirname(target_path) or ".",
        ".comparetool_delivery_",
        ".txt",
    )
    try:
        write_delivery_instructions(project_results, stage_path)
        return stage_path, target_path
    except BaseException:
        if os.path.isfile(stage_path):
            os.remove(stage_path)
        FileExporter._cleanup_stage(stage_path)
        raise


def write_delivery_instructions(project_results: list, output_path: str):
    delete_entries = []
    rename_entries = []
    permission_entries = []
    directory_delete_entries = []
    deleted_count = 0

    for project in project_results:
        project_name = str(project.get("project_name", "") or "").strip()
        result = project["diff_result"]
        directory_delete_entries.extend(
            _delivery_path(project_name, path)
            for path in result.required_directory_deletions
        )
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
            if file_diff.change_type != ChangeType.DELETED:
                delivery_path = _delivery_path(project_name, file_diff.file_path)
                if file_diff.new_executable is True:
                    permission_entries.append((delivery_path, "设置可执行权限（chmod +x）"))
                elif (
                    file_diff.old_executable is True
                    and file_diff.new_executable is False
                ):
                    permission_entries.append((delivery_path, "移除可执行权限（chmod -x）"))

    delete_entries.sort(key=lambda item: item[0].casefold())
    rename_entries.sort(key=lambda item: (item[0].casefold(), item[1].casefold()))
    permission_entries.sort(key=lambda item: item[0].casefold())
    directory_delete_entries = sorted(
        dict.fromkeys(directory_delete_entries), key=str.casefold
    )

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
        f"写入同名文件前需删除的旧目录：{len(directory_delete_entries)} 个",
        f"需要确认的可执行权限：{len(permission_entries)} 个",
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
        "二、写入同名文件前必须删除的旧目录",
        "==================================",
    ])
    if directory_delete_entries:
        lines.extend(
            f"[删除旧目录后再写入同名文件] {path}"
            for path in directory_delete_entries
        )
    else:
        lines.append("本次无目录替换为同名文件的情况。")

    lines.extend([
        "",
        "三、重命名对应关系",
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
        "四、Unix/Linux 可执行权限",
        "=======================",
    ])
    if permission_entries:
        lines.append("普通 Windows 文件目录不会保留 Git/SVN 的 Unix 可执行位，请在目标环境确认：")
        lines.extend(f"[{action}] {path}" for path, action in permission_entries)
    else:
        lines.append("本次无需要单独处理的可执行权限。")

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
