"""Reproduce folder generation via the real GUI in an isolated output directory."""
import argparse
import collections
import functools
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("gui", "direct"), default="gui")
    args = parser.parse_args()
    inputs = json.loads(Path(args.inputs).read_text(encoding="utf-8-sig"))
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    os.environ["COMPARETOOL_TEMP_DIR"] = str(destination / "runtime")
    os.environ["COMPARETOOL_TRANSACTION_KEY_FILE"] = str(destination / "key")
    import main as application
    import logger
    logger.LOG_FILE = str(destination / "application.log")
    application.CONFIG_DIR = str(destination)
    application.CONFIG_FILE = str(destination / "config.json")
    Path(application.CONFIG_FILE).write_text(json.dumps({
        "vcs_type": "folder", "output_dir": str(destination / "delivery"),
    }), encoding="utf-8")
    (destination / "delivery").mkdir()
    lock = threading.RLock()
    clock = time.perf_counter()
    aggregates = collections.defaultdict(lambda: [0, 0.0])
    callbacks = collections.Counter()
    events = (destination / "events.jsonl").open("w", encoding="utf-8")

    def record(event, **details):
        with lock:
            value = dict(event=event, elapsed=round(time.perf_counter()-clock, 6), **details)
            events.write(json.dumps(value, ensure_ascii=False) + "\n")
            events.flush()
            if event != "heartbeat":
                print(json.dumps(value, ensure_ascii=False), flush=True)

    def instrument(cls, name, verbose=True):
        descriptor = inspect.getattr_static(cls, name)
        method = descriptor.__func__ if isinstance(descriptor, (classmethod, staticmethod)) else descriptor
        label = cls.__name__ + "." + name
        @functools.wraps(method)
        def measured(*positional, **named):
            start = time.perf_counter()
            if verbose:
                record("begin", phase=label)
            try:
                return method(*positional, **named)
            finally:
                duration = time.perf_counter() - start
                with lock:
                    aggregates[label][0] += 1
                    aggregates[label][1] += duration
                if verbose:
                    record("end", phase=label, seconds=round(duration, 6))
        replacement = type(descriptor)(measured) if isinstance(descriptor, (classmethod, staticmethod)) else measured
        setattr(cls, name, replacement)

    for name in ("_capture_directory", "_compare_captures", "_snapshot_directory", "_verify_capture"):
        instrument(application.FolderVCS, name)
    instrument(application.DiffEngine, "generate_diff")
    instrument(application.ReportGenerator, "generate")
    for name in ("prepare_target_states", "prepare_export", "_replace_outputs"):
        instrument(application.FileExporter, name)
    instrument(application.FileExporter, "_tree_identity", verbose=False)
    original_callback = application.tk.CallWrapper.__call__
    def tracked_callback(wrapper, *values):
        code = getattr(wrapper.func, "__code__", None)
        if code is not None:
            callbacks[(Path(code.co_filename).name, code.co_firstlineno, code.co_name)] += 1
        return original_callback(wrapper, *values)
    application.tk.CallWrapper.__call__ = tracked_callback
    worker_finished = threading.Event()
    result = {"success": False, "mode": args.mode}

    class DiagnosticApp(application.CompareToolApp):
        def _confirm_output_batch(self, *values):
            return True
        def _on_complete(self, report_path, summary):
            result.update(success=True, report=report_path, summary=summary)
            self.progress.stop()
            record("complete_callback", files=summary["total_files"])
        def _show_error(self, message):
            result.update(error=str(message))
            self.progress.stop()
            record("generation_error", message=str(message))
        def _do_generate(self, *values, **options):
            start = time.perf_counter()
            try:
                return super()._do_generate(*values, **options)
            finally:
                result["worker_seconds"] = time.perf_counter() - start
                worker_finished.set()
        def _on_close(self):
            if not self._generating:
                self.root.destroy()

    app = DiagnosticApp()
    app.root.title("CompareTool - diagnostic run (separate output)")
    def heartbeat():
        record("heartbeat", callbacks=sum(callbacks.values()),
               top_callbacks=[(list(k),v) for k,v in callbacks.most_common(5)])
        if worker_finished.is_set():
            app.root.after(100, app.root.quit)
        else:
            app.root.after(2000, heartbeat)
    def start():
        app.old_version_var.set(inputs["old"])
        app.new_version_var.set(inputs["new"])
        app.project_name_var.set(inputs["project_name"])
        app._project_name_manual = True
        app.output_batch_var.set("diagnostic")
        app.show_full_context_var.set("yes" if inputs.get("show_full_context", True) else "no")
        app.exclude_text.delete("1.0", "end")
        app.exclude_text.insert("1.0", "\n".join(inputs.get("exclude_patterns", [])))
        app._refresh_output_paths_now()
        record("configured", frozen=bool(getattr(sys, "frozen", False)), python=sys.version)
        if args.mode == "gui":
            app._generate()
        else:
            app._do_generate("", "folder", inputs["old"], inputs["new"],
                inputs["project_name"], inputs.get("exclude_patterns", []),
                inputs.get("show_full_context", True), True,
                app.report_path_var.get(), app.old_export_var.get(),
                app.new_export_var.get(), app.output_dir_var.get())
        app.root.after(100, heartbeat)
    app.root.after(500, start)
    try:
        app.root.mainloop()
    finally:
        result.update(elapsed=time.perf_counter()-clock, phases=dict(aggregates),
                      callbacks=[(list(k),v) for k,v in callbacks.most_common(20)])
        (destination / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        record("finished", success=result["success"])
        events.close()
        app.root.destroy()
    return 0 if result["success"] else 1

if __name__ == "__main__":
    raise SystemExit(run())
