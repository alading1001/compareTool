import os
from datetime import datetime
from jinja2 import Environment, FileSystemLoader
from diff_engine import DiffResult


class ReportGenerator:
    """HTML报告生成器"""

    def __init__(self, template_dir: str = None):
        if template_dir is None:
            template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
        self.env = Environment(loader=FileSystemLoader(template_dir))

    def generate(self, diff_result: DiffResult, output_path: str, show_project_root: bool = True):
        summary = diff_result.summary
        template = self.env.get_template("report.html")
        html = template.render(
            project_name=diff_result.project_name,
            project_path=diff_result.project_path,
            vcs_type=diff_result.vcs_type,
            old_version=diff_result.old_version,
            new_version=diff_result.new_version,
            summary=summary,
            files=diff_result.files,
            show_project_root=show_project_root,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
