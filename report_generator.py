import os
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, select_autoescape
from diff_engine import DiffResult
from delivery_instructions import DELIVERY_INSTRUCTIONS_FILENAME


class ReportGenerator:
    """HTML报告生成器"""

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
        context = dict(
            project_name=diff_result.project_name,
            project_path=diff_result.project_path,
            vcs_type=diff_result.vcs_type,
            old_version=diff_result.old_version,
            new_version=diff_result.new_version,
            summary=summary,
            files=diff_result.report_files,
            show_project_root=show_project_root,
            delivery_instructions_name=delivery_instructions_name,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        stream = template.stream(**context)
        stream.enable_buffering(64)
        stream.dump(output_path, encoding="utf-8")

    def generate_multi(self, project_results: list, output_path: str):
        summary = self._multi_summary(project_results)
        template = self.env.get_template("multi_report.html")
        context = dict(
            summary=summary,
            projects=project_results,
            delivery_instructions_name=DELIVERY_INSTRUCTIONS_FILENAME,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        stream = template.stream(**context)
        stream.enable_buffering(64)
        stream.dump(output_path, encoding="utf-8")

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
