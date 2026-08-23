import difflib
import hashlib
import html
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional
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
    format_only: bool = False
    format_details: List[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return self.added_lines + self.deleted_lines

    @property
    def report_type(self) -> str:
        """报告展示类型；底层 change_type 保持不变，确保导出逻辑完整。"""
        if self.change_type == ChangeType.MODIFIED and self.format_only:
            return "F"
        return self.change_type.value


@dataclass
class _DecodedText:
    text: str
    encoding: str
    bom: str = "无"


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
        format_changed = sum(1 for f in self.files if f.report_type == "F")
        modified = sum(
            1 for f in self.files
            if f.change_type == ChangeType.MODIFIED and f.report_type != "F"
        )
        deleted = sum(1 for f in self.files if f.change_type == ChangeType.DELETED)
        renamed = sum(1 for f in self.files if f.change_type == ChangeType.RENAMED)
        total_added_lines = sum(f.added_lines for f in self.files)
        total_deleted_lines = sum(f.deleted_lines for f in self.files)
        return {
            "total_files": len(self.files),
            "added_files": added,
            "modified_files": modified,
            "format_changed_files": format_changed,
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
        if getattr(self.vcs, "merge_exact_renames", True):
            changed_files = self._merge_exact_renames(changed_files, old_version, new_version)

        result = DiffResult(
            project_path=self.vcs.project_path,
            project_name=os.path.basename(os.path.normpath(self.vcs.project_path)),
            vcs_type=type(self.vcs).__name__,
            old_version=old_version,
            new_version=new_version,
        )

        for cf in changed_files:
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
            file_diff.new_content = self.vcs.get_file_content(new_version, cf.path)
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
            old_raw = self._get_raw_bytes(old_version, old_path)
            new_raw = self._get_raw_bytes(new_version, cf.path)
            old_decoded = self._decode_text_strict(old_raw)
            new_decoded = self._decode_text_strict(new_raw)
            if old_decoded is not None and new_decoded is not None:
                file_diff.old_content = old_decoded.text
                file_diff.new_content = new_decoded.text
                format_details = self._format_only_details(
                    old_decoded, new_decoded, old_raw, new_raw)
                if format_details is not None:
                    file_diff.format_only = True
                    file_diff.format_details = format_details
            else:
                file_diff.old_content = self.vcs.get_file_content(old_version, old_path)
                file_diff.new_content = self.vcs.get_file_content(new_version, cf.path)

            old_lines = file_diff.old_content.splitlines()
            new_lines = file_diff.new_content.splitlines()
            file_diff.added_lines, file_diff.deleted_lines = self._count_line_changes(
                old_lines, new_lines)

            if old_raw is not None and new_raw is not None and old_raw == new_raw:
                file_diff.added_lines = 0
                file_diff.deleted_lines = 0
                file_diff.side_by_side_html = self._rename_only_placeholder(old_path, cf.path)
            elif file_diff.format_only:
                file_diff.added_lines = 0
                file_diff.deleted_lines = 0
                file_diff.side_by_side_html = self._rename_format_placeholder(
                    old_path, cf.path, file_diff.format_details)
            else:
                file_diff.side_by_side_html = self._side_by_side_html(
                    old_lines, new_lines, cf.path, old_path=old_path)

        else:
            old_raw = self._get_raw_bytes(old_version, cf.path)
            new_raw = self._get_raw_bytes(new_version, cf.path)
            old_decoded = self._decode_text_strict(old_raw)
            new_decoded = self._decode_text_strict(new_raw)

            if old_decoded is not None and new_decoded is not None:
                file_diff.old_content = old_decoded.text
                file_diff.new_content = new_decoded.text
                format_details = self._format_only_details(old_decoded, new_decoded, old_raw, new_raw)
                if format_details is not None:
                    file_diff.format_only = True
                    file_diff.format_details = format_details
            else:
                # 无法可靠解码时维持普通修改，绝不猜测成“仅格式变化”。
                file_diff.old_content = self.vcs.get_file_content(old_version, cf.path)
                file_diff.new_content = self.vcs.get_file_content(new_version, cf.path)

            old_lines = file_diff.old_content.splitlines()
            new_lines = file_diff.new_content.splitlines()

            file_diff.added_lines, file_diff.deleted_lines = self._count_line_changes(
                old_lines, new_lines)

            if file_diff.format_only:
                file_diff.added_lines = 0
                file_diff.deleted_lines = 0
                file_diff.side_by_side_html = self._format_only_placeholder(
                    cf.path, file_diff.format_details)
            else:
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
        data = self._get_raw_bytes(version, file_path)
        if data is None:
            return None
        return len(data), hashlib.sha256(data).hexdigest()

    def _get_raw_bytes(self, version: str, file_path: str) -> Optional[bytes]:
        getter = getattr(self.vcs, "get_file_content_raw_bytes", None)
        if getter is not None:
            return getter(version, file_path)
        return self.vcs.get_file_content_bytes(version, file_path)

    @staticmethod
    def _decode_text_strict(data: Optional[bytes]) -> Optional[_DecodedText]:
        """保守解码。只有严格解码成功时才参与格式变化判断。"""
        if data is None:
            return None

        bom_specs = (
            (b"\x00\x00\xfe\xff", "utf-32", "UTF-32 BE", "UTF-32 BOM"),
            (b"\xff\xfe\x00\x00", "utf-32", "UTF-32 LE", "UTF-32 BOM"),
            (b"\xef\xbb\xbf", "utf-8-sig", "UTF-8", "UTF-8 BOM"),
            (b"\xfe\xff", "utf-16", "UTF-16 BE", "UTF-16 BOM"),
            (b"\xff\xfe", "utf-16", "UTF-16 LE", "UTF-16 BOM"),
        )
        for prefix, codec, label, bom in bom_specs:
            if data.startswith(prefix):
                try:
                    return _DecodedText(data.decode(codec), label, bom)
                except UnicodeDecodeError:
                    return None

        if b"\x00" in data:
            return None

        try:
            text = data.decode("utf-8")
            label = "ASCII" if data.isascii() else "UTF-8"
            return _DecodedText(text, label)
        except UnicodeDecodeError:
            pass

        try:
            return _DecodedText(data.decode("gb18030"), "GB18030/GBK")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _line_ending(text: str) -> str:
        crlf = text.count("\r\n")
        rest = text.replace("\r\n", "")
        lf = rest.count("\n")
        cr = rest.count("\r")
        kinds = [("CRLF", crlf), ("LF", lf), ("CR", cr)]
        present = [name for name, count in kinds if count]
        if not present:
            return "无换行符"
        return present[0] if len(present) == 1 else "混合(" + "/".join(present) + ")"

    @staticmethod
    def _normalize_line_endings(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _format_only_details(
        self,
        old: _DecodedText,
        new: _DecodedText,
        old_raw: bytes,
        new_raw: bytes,
    ) -> Optional[List[str]]:
        if old_raw == new_raw:
            return None
        if old.text != new.text and (
            self._normalize_line_endings(old.text) != self._normalize_line_endings(new.text)
        ):
            return None

        details = []
        if old.encoding != new.encoding:
            details.append(f"编码：{old.encoding} → {new.encoding}")
        if old.bom != new.bom:
            details.append(f"BOM：{old.bom} → {new.bom}")
        old_eol = self._line_ending(old.text)
        new_eol = self._line_ending(new.text)
        if old_eol != new_eol:
            details.append(f"换行符：{old_eol} → {new_eol}")
        if not details:
            # 解码文本相同但无法可靠归因时，保守说明事实，不虚构编码名称。
            details.append("原始字节发生变化，文字内容一致")
        return details

    @staticmethod
    def _count_line_changes(old_lines: List[str], new_lines: List[str]):
        added = 0
        deleted = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                deleted += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
        return added, deleted

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

    def _format_only_placeholder(self, file_path: str, details: List[str]) -> str:
        """纯格式变化：不制造虚假的逐行内容差异。"""
        detail_html = "".join(
            f'<div style="margin-top:6px;">{html.escape(item)}</div>'
            for item in details
        )
        return (
            '<div style="padding:40px;text-align:center;color:#666;font-size:15px;">'
            '<div style="font-size:18px;margin-bottom:12px;font-weight:bold;">'
            '仅格式变化，文字内容无变化</div>'
            f'<div style="font-family:Consolas,\'Courier New\',monospace;margin-bottom:12px;">'
            f'{html.escape(file_path)}</div>'
            f'{detail_html}'
            '<div style="font-size:12px;margin-top:14px;color:#999;">'
            '文件原始字节已变化，仍会包含在导出的变更文件中</div>'
            '</div>'
        )

    def _rename_format_placeholder(self, old_path: str, new_path: str, details: List[str]) -> str:
        """重命名同时只有编码/BOM/换行变化。"""
        detail_html = "".join(
            f'<div style="margin-top:6px;">{html.escape(item)}</div>'
            for item in details
        )
        return (
            '<div style="padding:40px;text-align:center;color:#666;font-size:15px;">'
            '<div style="font-size:18px;margin-bottom:12px;font-weight:bold;">'
            '文件已重命名，同时发生格式变化</div>'
            f'<div style="font-family:Consolas,\'Courier New\',monospace;margin-bottom:12px;">'
            f'{html.escape(old_path)} &rarr; {html.escape(new_path)}</div>'
            f'{detail_html}'
            '<div style="font-size:12px;margin-top:14px;color:#999;">'
            '文字内容无变化，但导出的文件原始字节已变化</div>'
            '</div>'
        )

    def _side_by_side_html(self, old_lines, new_lines, path, old_path=None):
        """生成左右对比的HTML表格"""
        hd = difflib.HtmlDiff(tabsize=4)
        old_desc = old_path or path
        escaped_old_desc = html.escape(old_desc)
        escaped_path = html.escape(path)
        if self.show_full_context:
            return hd.make_table(
                old_lines, new_lines,
                fromdesc=f'旧版本: {escaped_old_desc}',
                todesc=f'新版本: {escaped_path}',
                context=False
            )
        else:
            return hd.make_table(
                old_lines, new_lines,
                fromdesc=f'旧版本: {escaped_old_desc}',
                todesc=f'新版本: {escaped_path}',
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
            todesc=f'新版本: {html.escape(path)}',
            context=False
        )

    def _side_by_side_old_vs_empty(self, old_content, path):
        """删除文件：左侧旧内容，右侧空"""
        old_lines = old_content.splitlines() if old_content else []
        new_lines = []
        hd = difflib.HtmlDiff(tabsize=4)
        return hd.make_table(
            old_lines, new_lines,
            fromdesc=f'旧版本: {html.escape(path)}',
            todesc='(已删除)',
            context=False
        )
