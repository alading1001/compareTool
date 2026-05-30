import difflib
import hashlib
import html
import os
from dataclasses import dataclass, field
from typing import List, Dict
from vcs.base import ChangedFile, ChangeType


@dataclass
class FileDiff:
    """单个文件的差异信息"""
    file_path: str
    change_type: ChangeType
    old_path: str = ""
    old_content: str = ""
    new_content: str = ""
    unified_diff: str = ""
    side_by_side_html: str = ""
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

    # 二进制/归档文件扩展名，不展示具体内容差异
    BINARY_EXTS = {
        ".jar", ".war", ".ear", ".aar",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".class", ".so", ".dll", ".exe", ".bin",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".ttf", ".otf", ".woff", ".woff2",
        ".mp3", ".mp4", ".avi", ".mov",
    }

    def __init__(self, vcs, show_full_context: bool = True):
        self.vcs = vcs
        self.show_full_context = show_full_context

    def _is_binary(self, file_path: str) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        return ext in self.BINARY_EXTS

    def generate_diff(self, old_version: str, new_version: str) -> DiffResult:
        """生成两个版本之间的完整差异"""
        changed_files = self.vcs.get_changed_files(old_version, new_version)
        changed_files = self._merge_exact_renames(changed_files, old_version, new_version)

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
        file_diff = FileDiff(
            file_path=cf.path,
            change_type=cf.change_type,
            old_path=cf.old_path,
        )

        # 二进制文件：只标记变更，不展示内容差异
        if self._is_binary(cf.path) or (cf.old_path and self._is_binary(cf.old_path)):
            file_diff.side_by_side_html = self._binary_placeholder(cf)
            return file_diff

        if cf.change_type == ChangeType.ADDED:
            file_diff.old_content = ""
            file_diff.new_content = self.vcs.get_file_content_working(cf.path)
            file_diff.deleted_lines = 0
            file_diff.added_lines = len(file_diff.new_content.splitlines()) if file_diff.new_content else 0
            file_diff.side_by_side_html = self._side_by_side_empty_vs_new(
                file_diff.new_content, cf.path)

        elif cf.change_type == ChangeType.DELETED:
            file_diff.old_content = self.vcs.get_file_content(old_version, cf.path)
            file_diff.new_content = ""
            file_diff.deleted_lines = len(file_diff.old_content.splitlines()) if file_diff.old_content else 0
            file_diff.added_lines = 0
            file_diff.side_by_side_html = self._side_by_side_old_vs_empty(
                file_diff.old_content, cf.path)

        elif cf.change_type == ChangeType.RENAMED:
            old_path = cf.old_path or cf.path
            file_diff.old_content = self.vcs.get_file_content(old_version, old_path)
            file_diff.new_content = self.vcs.get_file_content_working(cf.path)

            old_lines = file_diff.old_content.splitlines()
            new_lines = file_diff.new_content.splitlines()
            file_diff.added_lines = max(0, len(new_lines) - len(old_lines))
            file_diff.deleted_lines = max(0, len(old_lines) - len(new_lines))

            if self._same_file_bytes(old_version, new_version, old_path, cf.path):
                file_diff.added_lines = 0
                file_diff.deleted_lines = 0
                file_diff.side_by_side_html = self._rename_only_placeholder(old_path, cf.path)
            else:
                file_diff.side_by_side_html = self._side_by_side_html(
                    old_lines, new_lines, cf.path, old_path=old_path)

        else:
            file_diff.old_content = self.vcs.get_file_content(old_version, cf.path)
            file_diff.new_content = self.vcs.get_file_content_working(cf.path)

            old_lines = file_diff.old_content.splitlines()
            new_lines = file_diff.new_content.splitlines()

            # 统计行数
            file_diff.added_lines = max(0, len(new_lines) - len(old_lines))
            file_diff.deleted_lines = max(0, len(old_lines) - len(new_lines))

            # side-by-side HTML
            file_diff.side_by_side_html = self._side_by_side_html(
                old_lines, new_lines, cf.path)

        return file_diff

    def _merge_exact_renames(
        self,
        changed_files: List[ChangedFile],
        old_version: str,
        new_version: str,
    ) -> List[ChangedFile]:
        """将内容完全一致且唯一匹配的删除+新增合并为重命名。"""
        added = [f for f in changed_files if f.change_type == ChangeType.ADDED]
        deleted = [f for f in changed_files if f.change_type == ChangeType.DELETED]
        if not added or not deleted:
            return changed_files

        deleted_by_sig = {}
        added_by_sig = {}
        for item in deleted:
            sig = self._file_bytes_signature(old_version, item.path)
            if sig is not None:
                deleted_by_sig.setdefault(sig, []).append(item)
        for item in added:
            sig = self._file_bytes_signature(new_version, item.path)
            if sig is not None:
                added_by_sig.setdefault(sig, []).append(item)

        pairs_by_new_id = {}
        paired_old_ids = set()
        for sig, old_items in deleted_by_sig.items():
            new_items = added_by_sig.get(sig, [])
            if len(old_items) == 1 and len(new_items) == 1:
                old_item = old_items[0]
                new_item = new_items[0]
                paired_old_ids.add(id(old_item))
                pairs_by_new_id[id(new_item)] = old_item.path

        if not pairs_by_new_id:
            return changed_files

        result = []
        for item in changed_files:
            if id(item) in paired_old_ids:
                continue
            old_path = pairs_by_new_id.get(id(item))
            if old_path:
                result.append(ChangedFile(
                    path=item.path,
                    change_type=ChangeType.RENAMED,
                    old_path=old_path,
                ))
            else:
                result.append(item)
        return result

    def _file_bytes_signature(self, version: str, file_path: str):
        data = self.vcs.get_file_content_bytes(version, file_path)
        if data is None:
            return None
        return len(data), hashlib.sha256(data).hexdigest()

    def _same_file_bytes(self, old_version: str, new_version: str, old_path: str, new_path: str) -> bool:
        old_data = self.vcs.get_file_content_bytes(old_version, old_path)
        new_data = self.vcs.get_file_content_bytes(new_version, new_path)
        return old_data is not None and new_data is not None and old_data == new_data

    def _binary_placeholder(self, cf: ChangedFile) -> str:
        """二进制文件：占位提示"""
        ext = os.path.splitext(cf.path)[1].upper()
        label = {"A": "新增", "M": "修改", "D": "删除", "R": "重命名"}.get(cf.change_type.value, "变更")
        display_path = html.escape(cf.path)
        if cf.change_type == ChangeType.RENAMED and cf.old_path:
            display_path = f"{html.escape(cf.old_path)} &rarr; {display_path}"
        return (
            f'<div style="padding:40px;text-align:center;color:#888;font-size:15px;">'
            f'<div style="font-size:48px;margin-bottom:16px;">📦</div>'
            f'<div><b>{display_path}</b></div>'
            f'<div style="margin-top:8px;">{ext} 二进制归档文件 &mdash; {label}</div>'
            f'<div style="font-size:12px;margin-top:4px;color:#bbb;">不支持差异内容展示，仅标记文件变更状态</div>'
            f'</div>'
        )

    def _rename_only_placeholder(self, old_path: str, new_path: str) -> str:
        """纯重命名文件：内容无变化时显示明确提示。"""
        return (
            '<div style="padding:40px;text-align:center;color:#666;font-size:15px;">'
            '<div style="font-size:18px;margin-bottom:12px;font-weight:bold;">仅重命名，内容无变化</div>'
            f'<div style="font-family:Consolas,\'Courier New\',monospace;">'
            f'{html.escape(old_path)} &rarr; {html.escape(new_path)}'
            '</div>'
            '</div>'
        )

    def _side_by_side_html(self, old_lines, new_lines, path, old_path=None):
        """生成左右对比的HTML表格"""
        hd = difflib.HtmlDiff(tabsize=4)
        old_desc = old_path or path
        if self.show_full_context:
            return hd.make_table(
                old_lines, new_lines,
                fromdesc=f'旧版本: {old_desc}',
                todesc=f'新版本: {path}',
                context=False
            )
        else:
            return hd.make_table(
                old_lines, new_lines,
                fromdesc=f'旧版本: {old_desc}',
                todesc=f'新版本: {path}',
                context=True,
                numlines=3
            )

    def _side_by_side_empty_vs_new(self, new_content, path):
        """新增文件：左侧空，右侧新内容"""
        old_lines = []
        new_lines = new_content.splitlines() if new_content else []
        hd = difflib.HtmlDiff(tabsize=4)
        return hd.make_table(
            old_lines, new_lines,
            fromdesc='(新文件)',
            todesc=f'新版本: {path}',
            context=False
        )

    def _side_by_side_old_vs_empty(self, old_content, path):
        """删除文件：左侧旧内容，右侧空"""
        old_lines = old_content.splitlines() if old_content else []
        new_lines = []
        hd = difflib.HtmlDiff(tabsize=4)
        return hd.make_table(
            old_lines, new_lines,
            fromdesc=f'旧版本: {path}',
            todesc='(已删除)',
            context=False
        )
