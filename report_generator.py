import os
import tempfile
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, select_autoescape
from diff_engine import DiffResult
from delivery_instructions import DELIVERY_INSTRUCTIONS_FILENAME
from stage_ownership import mark_owned, remove_ownership_marker


class ReportGenerator:
    """HTML报告生成器"""

    # 默认完整流式写出报告，不因预计体积大而拒绝旧版本能够生成的任务。
    # 数值上限仅保留为显式策略/测试注入点。
    MAX_REPORT_OUTPUT_BYTES = None

    def __init__(self, template_dir: str = None):
        if template_dir is None:
            template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(enabled_extensions=("html", "xml")),
        )

    def generate(
        self,
        diff_result: DiffResult,
        output_path: str,
        show_project_root: bool = True,
        delivery_instructions_name: str = DELIVERY_INSTRUCTIONS_FILENAME,
    ):
        summary = diff_result.summary
        template = self.env.get_template("report.html")
        report_files = diff_result.report_files
        manifest_files = diff_result.report_manifest_files
        manifest_matches_files = (
            report_files is manifest_files
            or (
                len(report_files) == len(manifest_files)
                and all(
                    report_file is manifest_file
                    for report_file, manifest_file in zip(
                        report_files, manifest_files
                    )
                )
            )
        )
        context = dict(
            project_name=diff_result.project_name,
            project_path=diff_result.project_path,
            vcs_type=diff_result.vcs_type,
            old_version=diff_result.old_version,
            new_version=diff_result.new_version,
            summary=summary,
            files=report_files,
            manifest_files=manifest_files,
            manifest_matches_files=manifest_matches_files,
            show_project_root=show_project_root,
            delivery_instructions_name=delivery_instructions_name,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        stream = template.stream(**context)
        self._dump_limited(stream, output_path)

    def generate_multi(self, project_results: list, output_path: str):
        summary = self._multi_summary(project_results)
        manifest_matches_files = (
            DiffResult.MAX_REPORT_MANIFEST_FILES is None
            and DiffResult.MAX_REPORT_MANIFEST_PATH_BYTES is None
            and summary["report_omitted_files"] == 0
        )
        if manifest_matches_files:
            manifest_entries = ()
            manifest_listed_files = summary["total_files"]
        else:
            manifest_entries = self._multi_manifest(project_results)
            manifest_listed_files = len(manifest_entries)
        summary["manifest_listed_files"] = manifest_listed_files
        summary["manifest_omitted_files"] = max(
            0, summary["total_files"] - manifest_listed_files
        )
        template = self.env.get_template("multi_report.html")
        context = dict(
            summary=summary,
            projects=project_results,
            manifest_entries=manifest_entries,
            manifest_matches_files=manifest_matches_files,
            delivery_instructions_name=DELIVERY_INSTRUCTIONS_FILENAME,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        stream = template.stream(**context)
        self._dump_limited(stream, output_path)

    @classmethod
    def _dump_limited(cls, stream, output_path: str):
        """按最终 UTF-8 字节流执行统一上限，并原子提交渲染结果。"""
        parent = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".comparetool_report_", suffix=".html", dir=parent
        )
        total = 0
        try:
            mark_owned(temporary)
            stream.enable_buffering(64)
            with os.fdopen(fd, "wb") as target:
                fd = -1
                for chunk in stream:
                    payload = str(chunk).encode("utf-8")
                    if cls.MAX_REPORT_OUTPUT_BYTES is not None:
                        total += len(payload)
                        if total > cls.MAX_REPORT_OUTPUT_BYTES:
                            raise RuntimeError(
                                "最终 HTML 报告超过统一大小上限: "
                                f"{total} > {cls.MAX_REPORT_OUTPUT_BYTES} 字节"
                            )
                    target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, output_path)
            remove_ownership_marker(temporary)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.remove(temporary)
            except OSError:
                pass
            remove_ownership_marker(temporary)
            raise

    @staticmethod
    def _multi_summary(project_results: list) -> dict:
        summary = {
            "project_count": len(project_results),
            "total_files": 0,
            "added_files": 0,
            "modified_files": 0,
            "format_changed_files": 0,
            "deleted_files": 0,
            "renamed_files": 0,
            "required_directory_deletions": 0,
            "total_added_lines": 0,
            "total_deleted_lines": 0,
            "skipped_line_count_files": 0,
            "report_omitted_files": 0,
        }
        for project in project_results:
            s = project["diff_result"].summary
            for key in (
                "total_files", "added_files", "modified_files", "format_changed_files", "deleted_files",
                "renamed_files", "required_directory_deletions",
                "total_added_lines", "total_deleted_lines",
                "skipped_line_count_files", "report_omitted_files",
            ):
                summary[key] += s.get(key, 0)
        summary["line_counts_complete"] = summary["skipped_line_count_files"] == 0
        return summary

    @staticmethod
    def _multi_manifest(project_results: list) -> list:
        entries = []
        json_bytes = 0
        for project in project_results:
            project_name = project["project_name"]
            show_project_root = project["show_project_root"]
            for file_diff in project["diff_result"].files:
                if (
                    DiffResult.MAX_REPORT_MANIFEST_FILES is not None
                    or DiffResult.MAX_REPORT_MANIFEST_PATH_BYTES is not None
                ):
                    item_bytes = DiffResult._htmlsafe_json_bytes({
                        "project": project_name,
                        "showProjectRoot": bool(show_project_root),
                        "path": file_diff.file_path.replace("\\", "/"),
                        "oldPath": file_diff.old_path.replace("\\", "/"),
                        "type": file_diff.report_type,
                    }) + 24
                else:
                    item_bytes = 0
                if (
                    DiffResult.MAX_REPORT_MANIFEST_FILES is not None
                    and len(entries) >= DiffResult.MAX_REPORT_MANIFEST_FILES
                ) or (
                    DiffResult.MAX_REPORT_MANIFEST_PATH_BYTES is not None
                    and json_bytes + item_bytes
                    > DiffResult.MAX_REPORT_MANIFEST_PATH_BYTES
                ):
                    return entries
                entries.append({
                    "project_name": project_name,
                    "show_project_root": show_project_root,
                    "file": file_diff,
                })
                json_bytes += item_bytes
        return entries
