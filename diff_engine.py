import difflib
import os
from dataclasses import dataclass, field
from typing import List, Dict
from vcs.base import ChangedFile, ChangeType


@dataclass
class FileDiff:
    """单个文件的差异信息"""
    file_path: str
    change_type: ChangeType
    old_content: str = ""
    new_content: str = ""
    unified_diff: str = ""
    added_lines: int = 0
    deleted_lines: int = 0

    @property
    def total_changes(self) -> int:
        return self.added_lines + self.deleted_lines


@dataclass
class DiffResult:
    """完整差异比对结果"""
    project_path: str
    project_name: str
    vcs_type: str
    old_version: str
    new_version: str
    files: List[FileDiff] = field(default_factory=list)

    @property
    def summary(self) -> Dict:
        added = sum(1 for f in self.files if f.change_type == ChangeType.ADDED)
        modified = sum(1 for f in self.files if f.change_type == ChangeType.MODIFIED)
        deleted = sum(1 for f in self.files if f.change_type == ChangeType.DELETED)
        renamed = sum(1 for f in self.files if f.change_type == ChangeType.RENAMED)
        total_added_lines = sum(f.added_lines for f in self.files)
        total_deleted_lines = sum(f.deleted_lines for f in self.files)
        return {
            "total_files": len(self.files),
            "added_files": added,
            "modified_files": modified,
            "deleted_files": deleted,
            "renamed_files": renamed,
            "total_added_lines": total_added_lines,
            "total_deleted_lines": total_deleted_lines,
        }


class DiffEngine:
    """差异比对引擎"""

    def __init__(self, vcs):
        self.vcs = vcs

    def generate_diff(self, old_version: str, new_version: str) -> DiffResult:
        """生成两个版本之间的完整差异"""
        changed_files = self.vcs.get_changed_files(old_version, new_version)

        result = DiffResult(
            project_path=self.vcs.project_path,
            project_name=os.path.basename(os.path.normpath(self.vcs.project_path)),
            vcs_type=type(self.vcs).__name__,
            old_version=old_version,
            new_version=new_version,
        )

        for cf in changed_files:
            # 跳过目录（SVN 会把目录也作为变更项）
            full_path = os.path.join(self.vcs.project_path, cf.path)
            if os.path.isdir(full_path):
                continue
            file_diff = self._diff_file(old_version, new_version, cf)
            result.files.append(file_diff)

        return result

    def _diff_file(self, old_version: str, new_version: str, cf: ChangedFile) -> FileDiff:
        file_diff = FileDiff(file_path=cf.path, change_type=cf.change_type)

        if cf.change_type == ChangeType.ADDED:
            # 新文件，旧版本无内容
            file_diff.old_content = ""
            file_diff.new_content = self.vcs.get_file_content_working(cf.path)
            file_diff.deleted_lines = 0
            file_diff.added_lines = len(file_diff.new_content.split("\n")) if file_diff.new_content else 0
            file_diff.unified_diff = self._format_new_file(file_diff.new_content, cf.path)

        elif cf.change_type == ChangeType.DELETED:
            # 删除文件，新版本无内容
            file_diff.old_content = self.vcs.get_file_content(old_version, cf.path)
            file_diff.new_content = ""
            file_diff.deleted_lines = len(file_diff.old_content.split("\n")) if file_diff.old_content else 0
            file_diff.added_lines = 0
            file_diff.unified_diff = self._format_deleted_file(file_diff.old_content, cf.path)

        else:
            # 修改或重命名
            file_diff.old_content = self.vcs.get_file_content(old_version, cf.path)
            file_diff.new_content = self.vcs.get_file_content_working(cf.path)

            old_lines = file_diff.old_content.splitlines(keepends=True)
            new_lines = file_diff.new_content.splitlines(keepends=True)

            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{cf.path} ({old_version})",
                tofile=f"b/{cf.path} ({new_version})",
                lineterm=""
            ))

            file_diff.unified_diff = "\n".join(diff_lines)

            # 统计增删行数
            for line in diff_lines:
                if line.startswith("+") and not line.startswith("+++"):
                    file_diff.added_lines += 1
                elif line.startswith("-") and not line.startswith("---"):
                    file_diff.deleted_lines += 1

        return file_diff

    def _format_new_file(self, content: str, path: str) -> str:
        lines = [f"--- /dev/null", f"+++ b/{path} (new file)", "@@ -0,0 +1,{len(content.splitlines())} @@"]
        for line in content.splitlines():
            lines.append(f"+{line}")
        return "\n".join(lines)

    def _format_deleted_file(self, content: str, path: str) -> str:
        lines = [f"--- a/{path} (deleted)", f"+++ /dev/null", f"@@ -1,{len(content.splitlines())} +0,0 @@"]
        for line in content.splitlines():
            lines.append(f"-{line}")
        return "\n".join(lines)
