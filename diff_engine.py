import difflib
import hashlib
import html
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from vcs.base import BaseVCS, ChangedFile, ChangeType


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
    metadata_changes: List[str] = field(default_factory=list)
    old_executable: Optional[bool] = None
    new_executable: Optional[bool] = None
    old_mode: str = ""
    new_mode: str = ""
    line_counts_complete: bool = True
    report_detail_omitted: bool = False

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
    required_directory_deletions: List[str] = field(default_factory=list)

    MAX_REPORT_MANIFEST_FILES = 100_000
    MAX_REPORT_MANIFEST_PATH_BYTES = 20 * 1024 * 1024

    @property
    def report_files(self) -> List[FileDiff]:
        return [item for item in self.files if not item.report_detail_omitted]

    @property
    def report_manifest_files(self) -> List[FileDiff]:
        selected = []
        path_bytes = 0
        for item in self.files:
            item_bytes = len(item.file_path.encode("utf-8", errors="replace"))
            if item.old_path:
                item_bytes += len(item.old_path.encode("utf-8", errors="replace"))
            if (
                len(selected) >= self.MAX_REPORT_MANIFEST_FILES
                or path_bytes + item_bytes > self.MAX_REPORT_MANIFEST_PATH_BYTES
            ):
                break
            selected.append(item)
            path_bytes += item_bytes
        return selected

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
        skipped_line_count_files = sum(
            1 for f in self.files if not f.line_counts_complete
        )
        report_omitted_files = sum(
            1 for f in self.files if f.report_detail_omitted
        )
        manifest_listed_files = len(self.report_manifest_files)
        return {
            "total_files": len(self.files),
            "added_files": added,
            "modified_files": modified,
            "format_changed_files": format_changed,
            "deleted_files": deleted,
            "renamed_files": renamed,
            "required_directory_deletions": len(self.required_directory_deletions),
            "total_added_lines": total_added_lines,
            "total_deleted_lines": total_deleted_lines,
            "line_counts_complete": skipped_line_count_files == 0,
            "skipped_line_count_files": skipped_line_count_files,
            "report_omitted_files": report_omitted_files,
            "manifest_listed_files": manifest_listed_files,
            "manifest_omitted_files": len(self.files) - manifest_listed_files,
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
    MAX_TEXT_DIFF_BYTES = 5 * 1024 * 1024
    MAX_TEXT_DIFF_LINES = 10_000
    MAX_TEXT_DIFF_LINE_BYTES = 64 * 1024
    MAX_TEXT_DIFF_LINE_PRODUCT = 4_000_000
    MAX_TEXT_DIFF_CHARACTER_PRODUCT = 25_000_000
    MAX_TEXT_DIFF_COMBINED_WORK = 50_000_000
    MAX_REPORT_TEXT_BYTES = 50 * 1024 * 1024
    MAX_REPORT_TEXT_LINES = 100_000
    MAX_REPORT_RENDER_ROWS = 60_000
    MAX_REPORT_DETAIL_FILES = 5_000
    MAX_REPORT_PATH_BYTES = 5 * 1024 * 1024
    MAX_REPORT_HTML_BYTES = 80 * 1024 * 1024
    MAX_RENAME_SIGNATURE_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        vcs,
        show_full_context: bool = True,
        report_budget: Optional[dict] = None,
    ):
        self.vcs = vcs
        self.show_full_context = show_full_context
        self._owns_report_budget = report_budget is None
        self._report_budget = report_budget if report_budget is not None else {}
        self._initialize_report_budget(reset=self._owns_report_budget)

    def _initialize_report_budget(self, reset: bool = False):
        for key in (
            "text_bytes", "text_lines", "render_rows", "detail_files",
            "path_bytes", "html_bytes",
        ):
            if reset or key not in self._report_budget:
                self._report_budget[key] = 0

    def _is_binary(self, file_path: str) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        return ext in self.BINARY_EXTS

    def generate_diff(self, old_version: str, new_version: str) -> DiffResult:
        """生成两个版本之间的完整差异"""
        if self._owns_report_budget:
            self._initialize_report_budget(reset=True)
        changed_files = self.vcs.get_changed_files(old_version, new_version)
        if getattr(self.vcs, "merge_exact_renames", True):
            changed_files = self._merge_exact_renames(changed_files, old_version, new_version)

        required_directories = self._infer_required_directory_deletions(
            changed_files,
            getattr(self.vcs, "required_directory_deletions", []) or [],
        )

        result = DiffResult(
            project_path=self.vcs.project_path,
            project_name=os.path.basename(os.path.normpath(self.vcs.project_path)),
            vcs_type=type(self.vcs).__name__,
            old_version=old_version,
            new_version=new_version,
            required_directory_deletions=sorted(required_directories),
        )

        for cf in changed_files:
            entry_reason = self._reserve_report_entry(cf)
            if entry_reason:
                file_diff = FileDiff(
                    file_path=cf.path,
                    change_type=cf.change_type,
                    old_path=cf.old_path,
                    metadata_changes=list(cf.metadata_changes),
                    old_executable=cf.old_executable,
                    new_executable=cf.new_executable,
                    old_mode=cf.old_mode,
                    new_mode=cf.new_mode,
                    line_counts_complete=False,
                    report_detail_omitted=True,
                )
            else:
                file_diff = self._diff_file(old_version, new_version, cf)
                self._finalize_report_entry(file_diff)
            result.files.append(file_diff)

        return result

    def _diff_file(self, old_version: str, new_version: str, cf: ChangedFile) -> FileDiff:
        file_diff = FileDiff(
            file_path=cf.path,
            change_type=cf.change_type,
            old_path=cf.old_path,
            metadata_changes=list(cf.metadata_changes),
            old_executable=cf.old_executable,
            new_executable=cf.new_executable,
            old_mode=cf.old_mode,
            new_mode=cf.new_mode,
        )

        old_path = cf.old_path or cf.path
        old_raw = None
        new_raw = None

        # 二进制文件：只标记变更，不展示内容差异
        if self._is_binary(cf.path) or (cf.old_path and self._is_binary(cf.old_path)):
            file_diff.side_by_side_html = self._binary_placeholder(cf)
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff

        endpoint_sizes = []
        if cf.change_type != ChangeType.ADDED:
            endpoint_sizes.append(self._get_file_raw_size(old_version, old_path))
        if cf.change_type != ChangeType.DELETED:
            endpoint_sizes.append(self._get_file_raw_size(new_version, cf.path))
        known_sizes = [size for size in endpoint_sizes if size is not None]
        if self._has_reliable_file_size() and len(known_sizes) != len(endpoint_sizes):
            raise RuntimeError(
                f"无法在读取前确认文件大小，已中止以避免不受限内存占用: {cf.path}"
            )
        if any(size > self.MAX_TEXT_DIFF_BYTES for size in known_sizes):
            file_diff.side_by_side_html = self._large_file_placeholder_from_size(
                cf, max(known_sizes)
            )
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff

        if cf.change_type != ChangeType.ADDED:
            old_raw = self._get_raw_bytes(old_version, old_path)
        if cf.change_type != ChangeType.DELETED:
            new_raw = self._get_raw_bytes(new_version, cf.path)
        expected_raw = []
        if cf.change_type != ChangeType.ADDED:
            expected_raw.append(("旧版本", old_path, old_raw, endpoint_sizes[0]))
        if cf.change_type != ChangeType.DELETED:
            size_index = 0 if cf.change_type == ChangeType.ADDED else 1
            expected_raw.append(("新版本", cf.path, new_raw, endpoint_sizes[size_index]))
        for label, path, data, expected_size in expected_raw:
            if data is None:
                raise RuntimeError(f"无法读取{label}原始字节，已中止生成: {path}")
            if expected_size is not None and len(data) != expected_size:
                raise RuntimeError(
                    f"{label}文件大小与读取字节不一致，已中止生成: "
                    f"{path} ({expected_size} != {len(data)})"
                )
        raw_values = [data for data in (old_raw, new_raw) if data is not None]
        if any(len(data) > self.MAX_TEXT_DIFF_BYTES for data in raw_values):
            file_diff.side_by_side_html = self._large_file_placeholder(cf, raw_values)
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff
        if any(self._content_is_binary(data) for data in raw_values):
            file_diff.side_by_side_html = self._binary_placeholder(cf)
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff
        complexity_reason = next(
            (
                reason for data in raw_values
                if (reason := self._text_diff_complexity_reason(data))
            ),
            "",
        )
        if complexity_reason:
            file_diff.side_by_side_html = self._complexity_placeholder(
                cf, complexity_reason
            )
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff

        # 纯重命名或纯格式变化只需要线性检查和小型说明卡片，不应被为
        # SequenceMatcher/HtmlDiff 设置的乘积及渲染预算降级成普通修改。
        if cf.change_type in (ChangeType.MODIFIED, ChangeType.RENAMED):
            early_old_decoded = self._decode_text_strict(old_raw)
            early_new_decoded = self._decode_text_strict(new_raw)
            if early_old_decoded is not None and early_new_decoded is not None:
                if cf.change_type == ChangeType.RENAMED and old_raw == new_raw:
                    file_diff.side_by_side_html = self._rename_only_placeholder(
                        old_path, cf.path
                    )
                    self._prepend_metadata(file_diff)
                    return file_diff
                early_format_details = self._format_only_details(
                    early_old_decoded, early_new_decoded, old_raw, new_raw
                )
                if early_format_details is not None:
                    file_diff.format_only = True
                    file_diff.format_details = early_format_details
                    if cf.change_type == ChangeType.RENAMED:
                        file_diff.side_by_side_html = self._rename_format_placeholder(
                            old_path, cf.path, early_format_details
                        )
                    else:
                        file_diff.side_by_side_html = self._format_only_placeholder(
                            cf.path, early_format_details
                        )
                    self._prepend_metadata(file_diff)
                    return file_diff

        old_line_count = self._line_count_for_budget(old_raw)
        new_line_count = self._line_count_for_budget(new_raw)
        combined_work = self._combined_diff_work(old_raw, new_raw)
        if combined_work > self.MAX_TEXT_DIFF_COMBINED_WORK:
            file_diff.side_by_side_html = self._complexity_placeholder(
                cf,
                (
                    f"行对与行内字符组合工作量 {combined_work:,} 超过计算上限 "
                    f"{self.MAX_TEXT_DIFF_COMBINED_WORK:,}"
                ),
            )
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff
        character_product = self._character_diff_product(old_raw, new_raw)
        if character_product > self.MAX_TEXT_DIFF_CHARACTER_PRODUCT:
            file_diff.side_by_side_html = self._complexity_placeholder(
                cf,
                (
                    f"最长行字符工作量 {character_product:,} 超过计算上限 "
                    f"{self.MAX_TEXT_DIFF_CHARACTER_PRODUCT:,}"
                ),
            )
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff
        if (
            old_line_count
            and new_line_count
            and old_line_count * new_line_count > self.MAX_TEXT_DIFF_LINE_PRODUCT
        ):
            file_diff.side_by_side_html = self._complexity_placeholder(
                cf,
                (
                    f"新旧行数乘积 {old_line_count * new_line_count:,} 超过计算上限 "
                    f"{self.MAX_TEXT_DIFF_LINE_PRODUCT:,}"
                ),
            )
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff

        budget_reason = self._reserve_report_budget(
            raw_values, old_line_count, new_line_count
        )
        if budget_reason:
            file_diff.side_by_side_html = self._report_budget_placeholder(
                cf, budget_reason
            )
            file_diff.line_counts_complete = False
            self._prepend_metadata(file_diff)
            return file_diff

        if cf.change_type == ChangeType.ADDED:
            file_diff.old_content = ""
            decoded = self._decode_text_strict(new_raw)
            file_diff.new_content = (
                decoded.text if decoded is not None
                else self.vcs.get_file_content(new_version, cf.path)
            )
            file_diff.deleted_lines = 0
            file_diff.added_lines = len(file_diff.new_content.splitlines()) if file_diff.new_content else 0
            file_diff.side_by_side_html = self._side_by_side_empty_vs_new(
                file_diff.new_content, cf.path)

        elif cf.change_type == ChangeType.DELETED:
            decoded = self._decode_text_strict(old_raw)
            file_diff.old_content = (
                decoded.text if decoded is not None
                else self.vcs.get_file_content(old_version, cf.path)
            )
            file_diff.new_content = ""
            file_diff.deleted_lines = len(file_diff.old_content.splitlines()) if file_diff.old_content else 0
            file_diff.added_lines = 0
            file_diff.side_by_side_html = self._side_by_side_old_vs_empty(
                file_diff.old_content, cf.path)

        elif cf.change_type == ChangeType.RENAMED:
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

        self._prepend_metadata(file_diff)
        return file_diff

    @staticmethod
    def _content_is_binary(data: bytes) -> bool:
        if data is None:
            return False
        if data.startswith((
            b"\xef\xbb\xbf",
            b"\xff\xfe",
            b"\xfe\xff",
            b"\xff\xfe\x00\x00",
            b"\x00\x00\xfe\xff",
        )):
            return DiffEngine._decode_text_strict(data) is None
        sample = data[:8192]
        if b"\x00" in data:
            return True
        decoded = None
        for encoding in ("utf-8", "gb18030"):
            try:
                decoded = sample.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            return True
        control_count = sum(
            1 for char in decoded
            if ord(char) < 32 and char not in "\t\n\r\f"
        )
        return control_count > max(2, len(decoded) // 100)

    def _text_diff_complexity_reason(self, data: bytes) -> str:
        lines = data.splitlines()
        if len(lines) > self.MAX_TEXT_DIFF_LINES:
            return f"行数 {len(lines):,} 超过展示上限 {self.MAX_TEXT_DIFF_LINES:,}"
        longest = max((len(line) for line in lines), default=len(data))
        if longest > self.MAX_TEXT_DIFF_LINE_BYTES:
            return (
                f"单行 {longest:,} 字节超过展示上限 "
                f"{self.MAX_TEXT_DIFF_LINE_BYTES:,} 字节"
            )
        return ""

    @classmethod
    def _character_diff_product(
        cls, old_data: Optional[bytes], new_data: Optional[bytes]
    ) -> int:
        """保守估算 HtmlDiff 逐行字符匹配的最坏工作量。"""
        if old_data is None or new_data is None:
            return 0
        old_lines = old_data.splitlines()
        new_lines = new_data.splitlines()
        if not old_lines or not new_lines:
            return 0
        old_longest = max((len(line) for line in old_lines), default=0)
        new_longest = max((len(line) for line in new_lines), default=0)
        paired_lines = min(len(old_lines), len(new_lines))
        return old_longest * new_longest * paired_lines

    @classmethod
    def _combined_diff_work(
        cls, old_data: Optional[bytes], new_data: Optional[bytes]
    ) -> int:
        """限制 Differ 枚举行对后再执行行内比较的组合成本。"""
        if old_data is None or new_data is None:
            return 0
        old_lines = old_data.splitlines()
        new_lines = new_data.splitlines()
        if not old_lines or not new_lines:
            return 0
        longest = max(
            max((len(line) for line in old_lines), default=0),
            max((len(line) for line in new_lines), default=0),
            1,
        )
        return len(old_lines) * len(new_lines) * longest

    @staticmethod
    def _infer_required_directory_deletions(
        changed_files: List[ChangedFile], declared_directories
    ) -> List[str]:
        old_removed_paths = [
            (item.old_path or item.path).replace("\\", "/")
            for item in changed_files
            if item.change_type in (ChangeType.DELETED, ChangeType.RENAMED)
        ]
        new_file_paths = [
            item.path.replace("\\", "/")
            for item in changed_files
            if item.change_type in (ChangeType.ADDED, ChangeType.RENAMED)
        ]
        new_path_keys = {path.rstrip("/").casefold() for path in new_file_paths}
        required = set()
        for directory in declared_directories:
            parts = directory.replace("\\", "/").strip("/").split("/")
            prefix = []
            for part in parts:
                prefix.append(part)
                if "/".join(prefix).casefold() in new_path_keys:
                    required.add(directory)
                    break

        trie = {"children": {}, "descendant": ""}
        for old_path in old_removed_paths:
            node = trie
            old_parts = old_path.strip("/").split("/")
            for index, part in enumerate(old_parts):
                node = node["children"].setdefault(
                    part.casefold(), {"children": {}, "descendant": ""}
                )
                if index < len(old_parts) - 1 and not node["descendant"]:
                    node["descendant"] = old_path
        for new_path in new_file_paths:
            parts = new_path.strip("/").split("/")
            node = trie
            for part in parts:
                node = node["children"].get(part.casefold())
                if node is None:
                    break
            if node is not None and node["descendant"]:
                old_parts = node["descendant"].split("/")
                required.add("/".join(old_parts[:len(parts)]))
        return sorted(required)

    def _reserve_report_entry(self, cf: ChangedFile) -> str:
        path_bytes = len(cf.path.encode("utf-8", errors="replace"))
        if cf.old_path:
            path_bytes += len(cf.old_path.encode("utf-8", errors="replace"))
        projected_files = self._report_budget["detail_files"] + 1
        projected_paths = self._report_budget["path_bytes"] + path_bytes
        if projected_files > self.MAX_REPORT_DETAIL_FILES:
            return (
                f"报告文件明细数 {projected_files:,} 超过上限 "
                f"{self.MAX_REPORT_DETAIL_FILES:,}"
            )
        if projected_paths > self.MAX_REPORT_PATH_BYTES:
            return (
                f"报告路径文本 {projected_paths:,} 字节超过上限 "
                f"{self.MAX_REPORT_PATH_BYTES:,} 字节"
            )
        self._report_budget["detail_files"] = projected_files
        self._report_budget["path_bytes"] = projected_paths
        return ""

    def _finalize_report_entry(self, file_diff: FileDiff):
        html_bytes = len(file_diff.side_by_side_html.encode("utf-8"))
        projected = self._report_budget["html_bytes"] + html_bytes
        if projected <= self.MAX_REPORT_HTML_BYTES:
            self._report_budget["html_bytes"] = projected
            return
        file_diff.report_detail_omitted = True
        file_diff.old_content = ""
        file_diff.new_content = ""
        file_diff.unified_diff = ""
        file_diff.side_by_side_html = ""

    def _get_file_size(self, version: str, file_path: str) -> Optional[int]:
        getter = getattr(self.vcs, "get_file_size", None)
        if getter is None:
            return None
        try:
            size = getter(version, file_path)
        except (OSError, RuntimeError, ValueError):
            return None
        return size if isinstance(size, int) and size >= 0 else None

    def _get_file_raw_size(self, version: str, file_path: str) -> Optional[int]:
        getter = getattr(self.vcs, "get_file_raw_size", None)
        if getter is None:
            return self._get_file_size(version, file_path)
        try:
            size = getter(version, file_path)
        except (OSError, RuntimeError, ValueError):
            return None
        return size if isinstance(size, int) and size >= 0 else None

    def _has_reliable_file_size(self) -> bool:
        method = getattr(type(self.vcs), "get_file_size", None)
        return method is not None and method is not BaseVCS.get_file_size

    @classmethod
    def _line_count_for_budget(cls, data: Optional[bytes]) -> int:
        if data is None:
            return 0
        decoded = cls._decode_text_strict(data)
        if decoded is not None:
            return len(decoded.text.splitlines())
        return len(data.splitlines())

    def _reserve_report_budget(
        self,
        raw_values: List[bytes],
        old_line_count: int,
        new_line_count: int,
    ) -> str:
        text_bytes = sum(len(data) for data in raw_values)
        text_lines = old_line_count + new_line_count
        render_rows = max(old_line_count, new_line_count)
        checks = (
            (
                self._report_budget["text_bytes"] + text_bytes,
                self.MAX_REPORT_TEXT_BYTES,
                "文本总字节",
                "字节",
            ),
            (
                self._report_budget["text_lines"] + text_lines,
                self.MAX_REPORT_TEXT_LINES,
                "文本总行数",
                "行",
            ),
            (
                self._report_budget["render_rows"] + render_rows,
                self.MAX_REPORT_RENDER_ROWS,
                "预计渲染行数",
                "行",
            ),
        )
        for projected, limit, label, unit in checks:
            if projected > limit:
                return f"{label} {projected:,} {unit}超过报告上限 {limit:,} {unit}"
        self._report_budget["text_bytes"] += text_bytes
        self._report_budget["text_lines"] += text_lines
        self._report_budget["render_rows"] += render_rows
        return ""

    def _prepend_metadata(self, file_diff: FileDiff):
        if not file_diff.metadata_changes:
            return
        details = "".join(
            f'<div style="margin-top:5px;">{html.escape(item)}</div>'
            for item in file_diff.metadata_changes
        )
        banner = (
            '<div style="padding:12px 16px;margin-bottom:12px;'
            'background:#fff7e6;border:1px solid #f0b44d;border-radius:4px;'
            'color:#6b4b16;">'
            '<b>文件元数据变化</b>'
            f'{details}</div>'
        )
        file_diff.side_by_side_html = banner + file_diff.side_by_side_html

    def _large_file_placeholder(self, cf: ChangedFile, raw_values: List[bytes]) -> str:
        largest = max((len(data) for data in raw_values), default=0)
        return self._large_file_placeholder_from_size(cf, largest)

    def _large_file_placeholder_from_size(
        self, cf: ChangedFile, largest: int
    ) -> str:
        display_path = html.escape(cf.path)
        return (
            '<div style="padding:40px;text-align:center;color:#666;font-size:15px;">'
            '<div style="font-size:18px;margin-bottom:12px;font-weight:bold;">'
            '文件过大，已跳过逐行差异展示</div>'
            f'<div>{display_path}</div>'
            f'<div style="margin-top:8px;">最大端点大小：{largest:,} 字节；'
            f'展示上限：{self.MAX_TEXT_DIFF_BYTES:,} 字节</div>'
            '<div style="font-size:12px;margin-top:8px;color:#999;">'
            '文件仍会完整包含在 oldVersion/newVersion 导出中</div>'
            '</div>'
        )

    @staticmethod
    def _complexity_placeholder(cf: ChangedFile, reason: str) -> str:
        return (
            '<div style="padding:40px;text-align:center;color:#666;font-size:15px;">'
            '<div style="font-size:18px;margin-bottom:12px;font-weight:bold;">'
            '文本结构复杂，已跳过逐行差异展示</div>'
            f'<div>{html.escape(cf.path)}</div>'
            f'<div style="margin-top:8px;">{html.escape(reason)}</div>'
            '<div style="font-size:12px;margin-top:8px;color:#999;">'
            '文件仍会完整包含在 oldVersion/newVersion 导出中</div>'
            '</div>'
        )

    @staticmethod
    def _report_budget_placeholder(cf: ChangedFile, reason: str) -> str:
        return (
            '<div style="padding:40px;text-align:center;color:#666;font-size:15px;">'
            '<div style="font-size:18px;margin-bottom:12px;font-weight:bold;">'
            '报告展示预算已用尽，已跳过逐行差异展示</div>'
            f'<div>{html.escape(cf.path)}</div>'
            f'<div style="margin-top:8px;">{html.escape(reason)}</div>'
            '<div style="font-size:12px;margin-top:8px;color:#999;">'
            '文件仍会完整包含在 oldVersion/newVersion 导出中</div>'
            '</div>'
        )

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
                pairs_by_new_id[id(new_item)] = old_item

        if not pairs_by_new_id:
            return changed_files

        result = []
        for item in changed_files:
            if id(item) in paired_old_ids:
                continue
            old_item = pairs_by_new_id.get(id(item))
            if old_item:
                metadata_changes = list(dict.fromkeys(
                    list(old_item.metadata_changes) + list(item.metadata_changes)
                ))
                result.append(ChangedFile(
                    path=item.path,
                    change_type=ChangeType.RENAMED,
                    old_path=old_item.path,
                    metadata_changes=metadata_changes,
                    old_executable=old_item.old_executable,
                    new_executable=item.new_executable,
                    old_mode=old_item.old_mode,
                    new_mode=item.new_mode,
                ))
            else:
                result.append(item)
        return result

    def _file_bytes_signature(self, version: str, file_path: str):
        signature_getter = getattr(self.vcs, "get_file_signature", None)
        signature_method = getattr(type(self.vcs), "get_file_signature", None)
        if (
            signature_getter is not None
            and signature_method is not None
            and signature_method is not BaseVCS.get_file_signature
        ):
            return signature_getter(version, file_path)
        size = self._get_file_size(version, file_path)
        if size is not None and size > self.MAX_RENAME_SIGNATURE_BYTES:
            return None
        # 对正式 VCS，无法取得大小就不能冒险整块读取未知体积文件。测试桩
        # 和旧扩展没有 size 接口时保留兼容回退。
        if size is None and self._has_reliable_file_size():
            return None
        data = self._get_raw_bytes(version, file_path)
        if data is None:
            return None
        if len(data) > self.MAX_RENAME_SIGNATURE_BYTES:
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
        old_normalized = self._normalize_line_endings(old.text)
        new_normalized = self._normalize_line_endings(new.text)
        trailing_newline_only = (
            old.text.splitlines() == new.text.splitlines()
            and old_normalized.endswith("\n") != new_normalized.endswith("\n")
        )
        if old.text != new.text and old_normalized != new_normalized and not trailing_newline_only:
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
        if trailing_newline_only:
            details.append(
                "末尾换行："
                f"{'有' if old_normalized.endswith(chr(10)) else '无'} → "
                f"{'有' if new_normalized.endswith(chr(10)) else '无'}"
            )
        if not details:
            # 解码文本相同但无法可靠归因时，保守说明事实，不虚构编码名称。
            details.append("原始字节发生变化，文字内容一致")
        return details

    @staticmethod
    def _count_line_changes(old_lines: List[str], new_lines: List[str]):
        added = 0
        deleted = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=True).get_opcodes():
            if tag in ("replace", "delete"):
                deleted += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
        return added, deleted

    def _binary_placeholder(self, cf: ChangedFile) -> str:
        """二进制文件：占位提示"""
        ext = html.escape(os.path.splitext(cf.path)[1].upper())
        label = {"A": "新增", "M": "修改", "D": "删除", "R": "重命名"}.get(cf.change_type.value, "变更")
        display_path = html.escape(cf.path)
        if cf.change_type == ChangeType.RENAMED and cf.old_path:
            display_path = f"{html.escape(cf.old_path)} &rarr; {display_path}"
        return (
            f'<div style="padding:40px;text-align:center;color:#888;font-size:15px;">'
            f'<div style="font-size:48px;margin-bottom:16px;">📦</div>'
            f'<div><b>{display_path}</b></div>'
            f'<div style="margin-top:8px;">{ext or "无扩展名"} 二进制归档文件 &mdash; {label}</div>'
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
