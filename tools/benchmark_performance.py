"""Reproducible local benchmark; only synthetic inputs are created.

Use the same script with --source-root pointing to each source revision.
Results verify file bytes and all rendered lines, not only elapsed time.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from unittest import mock

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-root", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--repeats", type=int, default=3)
parser.add_argument("--git-files", type=int, default=10)
args = parser.parse_args()
if args.repeats < 1 or args.git_files < 1:
    parser.error("repeats and git-files must be positive")
source = args.source_root.resolve()
sys.path.insert(0, str(source))
sys.path.insert(1, str(source / "tests"))

from diff_engine import DiffEngine, DiffResult, FileDiff
from file_exporter import FileExporter
from report_generator import ReportGenerator
from vcs.git_vcs import GitVCS
from vcs.base import ChangeType
from test_complete_export_review_fixes import BytesVCS, TableRows


def git(repo, *command):
    return subprocess.run(["git", *command], cwd=repo, capture_output=True,
                          check=True, timeout=30).stdout


def render_case(lines):
    values = []
    for _ in range(args.repeats):
        old, new = b"abcdefghijA\n" * lines, b"abcdefghijB\n" * lines
        started = time.perf_counter()
        file = DiffEngine(BytesVCS(old, new)).generate_diff("old", "new").files[0]
        seconds = time.perf_counter() - started
        rows = TableRows(file.side_by_side_html)
        assert rows.side() == [(i + 1, "abcdefghijA") for i in range(lines)]
        assert rows.side(True) == [(i + 1, "abcdefghijB") for i in range(lines)]
        assert (file.deleted_lines, file.added_lines) == (lines, lines)
        values.append(seconds)
    return {"seconds": values, "median_seconds": statistics.median(values),
            "lines_each": lines, "content_and_counts_verified": True}

def repeated_edge_case():
    old = b"unchanged repeated line\n" * 8000 + b"AAAAAAAAAA\n" * 64
    new = b"unchanged repeated line\n" * 8000 + b"BBBBBBBBBB\n" * 64
    values = []
    for _ in range(args.repeats):
        started = time.perf_counter()
        file = DiffEngine(BytesVCS(old, new)).generate_diff("old", "new").files[0]
        values.append(time.perf_counter() - started)
        rows = TableRows(file.side_by_side_html)
        assert rows.side() == list(enumerate(old.decode().splitlines(), 1))
        assert rows.side(True) == list(enumerate(new.decode().splitlines(), 1))
        assert (file.deleted_lines, file.added_lines) == (64, 64)
    return {"seconds": values, "median_seconds": statistics.median(values),
            "lines_each": 8064, "content_and_counts_verified": True}


def archive_stream_case():
    if "archive_details" not in FileDiff.__dataclass_fields__:
        return {"supported": False, "note": "This source revision predates recursive archive reports."}
    leaves = [FileDiff(f"member_{i}.txt", ChangeType.MODIFIED,
                side_by_side_html=f"<div>payload-{i:03d}:" + "x" * 131072 + "</div>")
              for i in range(64)]
    inner = DiffResult("input", "Demo", "folder", "old", "new", files=leaves)
    parent = FileDiff("app.jar", ChangeType.MODIFIED, archive_details=dict(
        status="compared", members=leaves, counts=inner.summary, filtered=False))
    result = DiffResult("input", "Demo", "folder", "old", "new", files=[parent],
                        archive_details_enabled=True)

    class MeasuredReport(ReportGenerator):
        def _dump_limited(self, stream, path):
            stream.enable_buffering(64)
            total = largest = chunks = 0
            for chunk in stream:
                payload = str(chunk).encode("utf-8")
                total += len(payload)
                largest = max(largest, len(payload))
                chunks += 1
            self.measurement = dict(total_bytes=total, largest_chunk_bytes=largest,
                                    chunks=chunks)

    runs = []
    generator = MeasuredReport(str(source / "templates"))
    for name in generator.env.list_templates():
        generator.env.get_template(name)
    for _ in range(args.repeats):
        tracemalloc.start()
        generator.generate(result, "unused")
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        runs.append(dict(generator.measurement, peak_additional_bytes=peak))
    return {"members": len(leaves), "runs": runs,
            "note": "Python rendering allocations only; input FileDiff HTML already exists; no disk I/O."}


def git_case(root):
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet", "--template=")
    for key, value in (("user.name", "CompareTool benchmark"),
                       ("user.email", "benchmark@example.invalid"),
                       ("core.autocrlf", "false"), ("core.eol", "lf")):
        git(repo, "config", key, value)
    names = [f"File {i:04d}.txt" for i in range(args.git_files)]
    old_bytes = {name: f"file {i}\nold value\n".encode() for i, name in enumerate(names)}
    new_bytes = {name: f"file {i}\nnew value\n".encode() for i, name in enumerate(names)}
    for name, payload in old_bytes.items():
        (repo / name).write_bytes(payload)
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "old")
    old = git(repo, "rev-parse", "HEAD").strip().decode()
    for name, payload in new_bytes.items():
        (repo / name).write_bytes(payload)
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "new")
    new = git(repo, "rev-parse", "HEAD").strip().decode()
    runs = []
    for index in range(args.repeats):
        output = root / f"run_{index}"
        output.mkdir()
        counts = Counter()
        original_popen = subprocess.Popen
        def counted(command, *positional, **keywords):
            counts["cat-file --batch" if "--batch" in command else str(command[1])] += 1
            return original_popen(command, *positional, **keywords)
        vcs, pairs = GitVCS(str(repo)), []
        try:
            with mock.patch("subprocess.Popen", side_effect=counted):
                started = time.perf_counter()
                result = DiffEngine(vcs).generate_diff(old, new)
                after_diff = time.perf_counter()
                ReportGenerator(str(source / "templates")).generate(result, str(output / "report.html"))
                after_report = time.perf_counter()
                pairs = FileExporter(result, vcs).prepare_export(
                    str(output / "old"), str(output / "new"), trusted_root=str(output))
                after_export = time.perf_counter()
            assert len(result.files) == args.git_files
            for stage, target in pairs:
                expected = old_bytes if Path(target).name == "old" else new_bytes
                actual = {p.relative_to(stage).as_posix(): p.read_bytes()
                          for p in Path(stage).rglob("*") if p.is_file()}
                assert actual == expected
            for file in result.files:
                rows = TableRows(file.side_by_side_html)
                assert [s for _, s in rows.side()] == old_bytes[file.file_path].decode().splitlines()
                assert [s for _, s in rows.side(True)] == new_bytes[file.file_path].decode().splitlines()
            runs.append({"diff_seconds": after_diff - started,
                         "report_seconds": after_report - after_diff,
                         "export_staging_seconds": after_export - after_report,
                         "total_seconds": after_export - started, "commands": dict(counts)})
        finally:
            FileExporter.cleanup_stages(pairs)
            vcs.cleanup()
    return {"files": args.git_files, "runs": runs,
            "median_seconds": statistics.median(r["total_seconds"] for r in runs),
            "export_bytes_and_report_content_verified": True,
            "note": "Diff + HTML write + staged file export; final transaction is not timed."}


(source / ".tmp").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="perf_bench_", dir=source / ".tmp") as temporary:
    working = Path(temporary).resolve()
    with mock.patch.dict(os.environ, {
        "COMPARETOOL_TEMP_DIR": str(working / "runtime"),
        "COMPARETOOL_TRANSACTION_KEY_FILE": str(working / "transaction.key"),
    }):
        results = {"source_root": str(source), "python": sys.version,
                   "repeats": args.repeats, "render_120": render_case(120),
                   "render_240": render_case(240),
                   "repeated_edges": repeated_edge_case(),
                   "archive_stream": archive_stream_case(), "git": git_case(working)}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results, ensure_ascii=False, indent=2))
