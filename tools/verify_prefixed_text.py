"""用实际新旧文件夹验证特殊头部展示、正文行数及完整导出字节。"""
import argparse
import copy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from diff_engine import DiffEngine
from main import CompareToolApp
from report_generator import ReportGenerator
from test_complete_export_review_fixes import TableRows


def inventory(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class HeaderCells(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.cells, self.value, self.inside = [], None, False
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "table" and dict(attrs).get("class") == "text-header-bytes":
            self.inside = True
        elif tag == "td" and self.inside:
            self.value = []

    def handle_data(self, value):
        if self.value is not None:
            self.value.append(value)

    def handle_endtag(self, tag):
        if tag == "td" and self.value is not None:
            self.cells.append("".join(self.value))
            self.value = None
        elif tag == "table":
            self.inside = False


def verify_worker(output, old, new, *, full):
    output.mkdir()
    app = CompareToolApp.__new__(CompareToolApp)
    app.root = mock.Mock()
    app.root.after.side_effect = lambda _ms, callback: callback()
    app._on_complete = mock.Mock()
    app._show_error = mock.Mock()
    app._task_log_dir = output / "jobs"
    captured = []
    generate = ReportGenerator.generate

    def observe(generator, result, *args, **kwargs):
        generate(generator, result, *args, **kwargs)
        # 在桌面工作线程释放片段之前保存这次实际生成的明细。
        result = copy.copy(result)
        result.files = [copy.copy(file) for file in result.files]
        for file in result.files:
            if file.html_fragment:
                file.side_by_side_html = "".join(file.html_fragment.iter_text())
                file.html_fragment = None
        captured.append(result)

    with mock.patch.object(ReportGenerator, "generate", observe):
        app._do_generate("", "folder", str(old), str(new), "特殊头部验证", [], full,
                         True, str(output / "report.html"), str(output / "oldVersion"),
                         str(output / "newVersion"), str(output), recursive_archives=False)
    assert not app._show_error.called, app._show_error.call_args
    assert app._last_task_record["success"], app._last_task_record
    assert len(captured) == 1
    result = captured[0]
    assert not result.issues, result.issues
    checks = []
    for file in result.files:
        old_path = old / (file.old_path or file.file_path)
        new_path = new / file.file_path
        a, b = old_path.read_bytes(), new_path.read_bytes()
        for side, original in (("oldVersion", a), ("newVersion", b)):
            assert (output / side / result.project_name / file.file_path).read_bytes() == original, (side, file.file_path)
        if Path(file.file_path).suffix.lower() != ".js":
            continue
        da, db = DiffEngine._decode_text_strict(a), DiffEngine._decode_text_strict(b)
        assert da.header == a[:32] and db.header == b[:32], file.file_path
        assert file.line_counts_complete and not file.format_only, file.file_path
        text = file.side_by_side_html
        assert "特殊头部字节（已变化）" in text and "header-byte-changed" in text, file.file_path
        assert "二进制归档文件" not in text and "\x00" not in text, file.file_path
        cells = HeaderCells(text).cells
        assert bytes.fromhex(" ".join(cells[1::3])) == a[:32], file.file_path
        assert bytes.fromhex(" ".join(cells[2::3])) == b[:32], file.file_path
        rows = TableRows(text)
        for is_new, decoded in ((False, da), (True, db)):
            actual = rows.side(is_new)
            expected = list(enumerate(DiffEngine._split_diff_lines(decoded.text), 1))
            # HTML 展示按现有规则展开制表符；对照每行而非仅检索片段。
            expected = [(number, line.expandtabs(4)) for number, line in expected]
            if full:
                assert actual == expected, (file.file_path, is_new, "完整正文")
            else:
                expected_by_number = dict(expected)
                assert all(expected_by_number[number] == line for number, line in actual), file.file_path
        if file.file_path == "ChangePin/src/ChangePin.js":
            assert any("<jydm>0131</jydm>" in line for _, line in rows.side()), "旧业务代码"
            assert any("<jydm>0143</jydm>" in line for _, line in rows.side(True)), "新业务代码"
        checks.append(dict(path=file.file_path, old_header=a[:32].hex(" "),
                           new_header=b[:32].hex(" "), body_added_lines=file.added_lines,
                           body_deleted_lines=file.deleted_lines))
    assert checks, "实际文件中没有验证到带头 .js"
    generator = ReportGenerator()
    generator.generate_multi([dict(project_name=result.project_name, vcs_type="folder",
                                   show_project_root=True, diff_result=result)],
                             str(output / "multi_report.html"))
    return dict(summary=result.summary, js_checks=checks,
                export_hashes={side: inventory(output / side) for side in ("oldVersion", "newVersion")},
                single_report=str(output / "report.html"), multi_report=str(output / "multi_report.html"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    old, new, output = args.old.resolve(), args.new.resolve(), args.output.resolve()
    assert old.is_dir() and new.is_dir()
    # 输出必须独立于两端输入，验证脚本不覆盖历史报告。
    for source in (old, new):
        assert not output.is_relative_to(source) and not source.is_relative_to(output)
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / "runtime"
    runtime.mkdir()
    os.environ.update(TEMP=str(runtime), TMP=str(runtime), COMPARETOOL_TEMP_DIR=str(runtime))
    import tempfile
    tempfile.tempdir = str(runtime)
    import logger
    logger.LOG_FILE = str(output / "verification.log")
    before = {"old": inventory(old), "new": inventory(new)}
    cases = {name: verify_worker(output / name, old, new, full=full)
             for name, full in (("full", True), ("context", False))}
    after = {"old": inventory(old), "new": inventory(new)}
    assert before == after, "验证期间输入文件发生变化"
    receipt = dict(source_old=str(old), source_new=str(new), inputs_unchanged=True,
                   input_sha256=before, cases=cases)
    (output / "verification.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(output=str(output), inputs_unchanged=True,
                          cases={name: dict(summary=case["summary"], js_files=len(case["js_checks"]))
                                 for name, case in cases.items()}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
