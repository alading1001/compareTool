"""Compare source revisions through the folder GUI worker in fresh output roots.

Inputs are read-only. Every run writes into a new directory under --output-parent;
timing includes empty-target clearing, generation, publication and worker cleanup.
Byte/visible-report verification happens after timing. No existing output is reused.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def worker(args):
    source = args.source_root.resolve()
    output = args.worker_output.resolve()
    output.mkdir(exist_ok=False)
    os.environ["COMPARETOOL_TEMP_DIR"] = str(output / "runtime")
    sys.path[:0] = [str(source), str(source / "tests")]
    from unittest import mock
    import logger
    from main import CompareToolApp
    from test_complete_export_review_fixes import TableRows

    logger.LOG_FILE = str(output / "application.log")
    project = args.new.name
    delivery = output / "delivery"
    delivery.mkdir()
    report = delivery / (project + "_diff.html")
    instructions = delivery / (project + "_上线操作说明.txt")
    app = CompareToolApp.__new__(CompareToolApp)
    app.root = mock.Mock()
    app._task_log_dir = output / "logs"

    def generate():
        app._do_generate(
            str(args.new), "folder", str(args.old), str(args.new), project, [],
            True, True, str(report), str(delivery / "oldVersion"),
            str(delivery / "newVersion"), str(delivery), recursive_archives=False,
        )

    if args.profile:
        import cProfile
        import pstats
        profiler = cProfile.Profile()
        profiler.runcall(generate)
        profiler.dump_stats(str(output / "profile.pstats"))
        with (output / "profile.txt").open("w", encoding="utf-8") as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(65)
    else:
        generate()
    record = app._last_task_record
    write_json(output / "task.json", record)
    if not record["success"]:
        raise RuntimeError(f"Worker failed; see {output / 'task.json'}")
    exported = {}
    for side in ("oldVersion", "newVersion"):
        base = delivery / side / project
        for path in sorted(base.rglob("*")):
            if path.is_file():
                exported[side + "/" + path.relative_to(base).as_posix()] = {
                    "bytes": path.stat().st_size, "sha256": digest(path),
                }
    rows = TableRows(report.read_text(encoding="utf-8"))
    visible = json.dumps([rows.side(), rows.side(True)], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    semantics = {
        "delivery": exported,
        "visible_text_sha256": hashlib.sha256(visible).hexdigest(),
        "instructions_sha256": digest(instructions),
        "counts": record["counts"],
    }
    result = dict(source=str(source), seconds=record["seconds"],
                  phases=record["phases"], semantics=semantics)
    write_json(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--output-parent", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--profile", action="store_true", help="Diagnostic only; timings include profiler overhead")
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.old, args.new = args.old.resolve(), args.new.resolve()
    if args.repeats < 1 or not args.old.is_dir() or not args.new.is_dir():
        parser.error("Require two existing input directories and repeats >= 1")
    if args.worker_output:
        destination = args.worker_output.resolve()
        if any(destination == root or root in destination.parents for root in (args.old, args.new)):
            parser.error("The benchmark output must be outside both input trees")
        worker(args)
        return
    parent = (args.output_parent or args.source_root / ".tmp").resolve()
    for input_root in (args.old, args.new):
        if parent == input_root or input_root in parent.parents:
            parser.error("The benchmark output must be outside both input trees")
    parent.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="folder_worker_", dir=parent))
    print(evidence, flush=True)
    sources = [("candidate", args.source_root.resolve())]
    if args.baseline_source:
        sources.insert(0, ("baseline", args.baseline_source.resolve()))
    results = []
    expected = None
    for repeat in range(args.repeats):
        # Alternate order to reduce systematic warm-cache/order bias.
        for label, source in (sources if repeat % 2 == 0 else list(reversed(sources))):
            destination = evidence / f"{repeat + 1}_{label}"
            command = [sys.executable, "-B", str(Path(__file__).resolve()),
                       "--old", str(args.old), "--new", str(args.new),
                       "--source-root", str(source), "--worker-output", str(destination)]
            if args.profile:
                command.append("--profile")
            subprocess.run(command, check=True, env={**os.environ, "PYTHONUTF8": "1"})
            result = json.loads((destination / "result.json").read_text(encoding="utf-8"))
            if expected is None:
                expected = result["semantics"]
            elif result["semantics"] != expected:
                raise AssertionError(f"Report/export semantics differ: {destination}")
            result.pop("semantics")
            result.update(label=label, repeat=repeat + 1, result=str(destination / "result.json"),
                          delivery_and_visible_report_verified=True)
            results.append(result)
            write_json(evidence / "runs.json", results)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    summary = {
        "old": str(args.old), "new": str(args.new), "profiled": args.profile,
        "recursive_archives": False, "exclude_patterns": [], "full_context": True,
        "timing": "GUI worker including empty-output clearing, publication and cleanup; verification excluded",
        "medians": {label: statistics.median(r["seconds"] for r in results if r["label"] == label)
                    for label, _ in sources},
        "delivery_files": len(expected["delivery"]),
        "delivery_bytes": sum(value["bytes"] for value in expected["delivery"].values()),
        "all_semantics_equal": True,
    }
    write_json(evidence / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
