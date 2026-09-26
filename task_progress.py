"""Task-local observation only: no file-content, comparison or transaction policy."""
import json
import inspect
import tempfile
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from app_version import build_info

_current = ContextVar("comparetool_task", default=None)


class ProgressMailbox:
    """One latest value, not an unbounded queue; the UI polls on its own thread."""
    def __init__(self):
        self._lock = threading.Lock()
        self._value = None

    def publish(self, value):
        with self._lock:
            self._value = value

    def latest(self):
        with self._lock:
            return self._value


class TaskObserver:
    MAX_EVENT_BYTES = 512 * 1024

    def __init__(self, mode, mailbox=None, log_dir=None, clock=time.perf_counter):
        self.clock, self.started = clock, clock()
        self.mode, self.mailbox = mode, mailbox or ProgressMailbox()
        self.task_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:12]
        self.stack, self.phases, self.summary = [], {}, {}
        self.completion, self.failure = None, None
        self.log_error, self.log_path, self.stream = "", "", None
        self.event_bytes, self.last_update = 0, -float("inf")
        self.project = ""
        if log_dir:
            try:
                directory = Path(log_dir)
                directory.mkdir(parents=True, exist_ok=True)
                self.log_path = str(directory / ("task_" + self.task_id + ".jsonl"))
                self.stream = open(self.log_path, "x", encoding="utf-8", buffering=1)
            except OSError as exc:
                self.log_error = type(exc).__name__
        self.record("task_start", mode=mode, build=build_info())

    def record(self, event, **fields):
        if self.stream is None:
            return
        try:
            payload = json.dumps(dict(schema="comparetool.task.v1", event=event,
                task_id=self.task_id, elapsed=round(self.clock()-self.started, 6), **fields),
                ensure_ascii=False) + "\n"
            size = len(payload.encode("utf-8"))
            if self.event_bytes + size <= self.MAX_EVENT_BYTES or event == "task_end":
                self.stream.write(payload)
                self.event_bytes += size
        except (OSError, ValueError, TypeError) as exc:
            self.log_error = type(exc).__name__
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None

    def publish(self, force=False):
        now = self.clock()
        if not force and now - self.last_update < 0.2:
            return
        self.last_update = now
        item = self.stack[-1] if self.stack else {}
        self.mailbox.publish({"label": item.get("label", "正在准备任务"),
            "done": item.get("done"), "total": item.get("total"),
            "project": self.project, "started": self.started,
            "log_error": self.log_error})

    def finish(self):
        result = dict(schema="comparetool.task.v1", task_id=self.task_id,
            mode=self.mode, build=build_info(), success=self.failure is None
            and bool(self.completion) and self.completion[0] != "_show_error",
            seconds=round(self.clock()-self.started, 6), phases=self.phases,
            counts=self.summary, error_type=self.failure, log_error=self.log_error)
        self.record("task_end", result=result)
        if self.stream:
            try:
                self.stream.close()
            except OSError as exc:
                self.log_error = type(exc).__name__
            self.stream = None
        if self.log_path:
            try:
                final = Path(self.log_path).with_suffix(".json")
                with open(final, "x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
                self._prune(final.parent)
            except (OSError, ValueError) as exc:
                self.log_error = type(exc).__name__
        result.update(log_path=self.log_path, log_error=self.log_error)
        return result

    @staticmethod
    def _prune(directory):
        # Only our completed task pairs; incomplete logs are kept as evidence.
        names = sorted(p for p in directory.glob("task_*.json") if re.fullmatch(
            r"task_\d{8}_\d{6}_[0-9a-f]{12}\.json", p.name))
        for path in names[:-50]:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("schema") == "comparetool.task.v1":
                    path.with_suffix(".jsonl").unlink(missing_ok=True)
                    path.unlink()
            except (OSError, ValueError):
                pass


@contextmanager
def stage(key, label):
    observer = _current.get()
    if observer is None:
        yield
        return
    item = dict(key=key, label=label, started=observer.clock(), children=0.0)
    observer.stack.append(item)
    observer.record("phase_start", phase=key, depth=len(observer.stack))
    observer.publish(force=True)
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        seconds = observer.clock() - item["started"]
        observer.stack.pop()
        if observer.stack:
            observer.stack[-1]["children"] += seconds
        totals = observer.phases.setdefault(key, dict(calls=0, seconds=0.0, exclusive_seconds=0.0))
        totals["calls"] += 1
        totals["seconds"] += seconds
        totals["exclusive_seconds"] += max(0, seconds-item["children"])
        observer.record("phase_end", phase=key, seconds=seconds, failed=failed,
                        done=item.get("done"), total=item.get("total"))
        observer.publish(force=True)


def measured_phase(key, label):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(key, label):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def progress(done=None, total=None, label=None):
    observer = _current.get()
    if observer is None or not observer.stack:
        return
    item = observer.stack[-1]
    item.update(done=done, total=total)
    if label:
        item["label"] = label
    observer.publish()


def advance():
    observer = _current.get()
    if observer and observer.stack:
        item = observer.stack[-1]
        progress((item.get("done") or 0) + 1, item.get("total"))


def project_progress(index, total):
    observer = _current.get()
    if observer:
        observer.project = f"项目 {index}/{total}"
        observer.publish(force=True)


def task_failure(exception):
    observer = _current.get()
    if observer:
        observer.failure = type(exception).__name__


def defer_completion(app, method, *args):
    observer = _current.get()
    if observer is None:
        app.root.after(0, lambda: getattr(app, method)(*args))
        return
    if method != "_show_error" and len(args) > 1:
        observer.summary.update({k: v for k, v in args[1].items()
                                 if isinstance(v, (int, float, bool))})
    observer.completion = method, args


def observe_job(kind):
    def decorate(function):
        @wraps(function)
        def wrapped(app, *args, **kwargs):
            mode = kwargs.get("vcs_type", args[1] if kind == "single" and len(args) > 1 else kind)
            bound = inspect.signature(function).bind(app, *args, **kwargs).arguments
            log_dir = select_log_dir(getattr(app, "_task_log_dir", None), bound)
            observer = TaskObserver(mode, getattr(app, "_task_mailbox", None), log_dir)
            token = _current.set(observer)
            try:
                return function(app, *args, **kwargs)
            except Exception as exc:
                task_failure(exc)
                observer.completion = "_show_error", (str(exc),)
            finally:
                try:
                    app._last_task_record = observer.finish()
                finally:
                    _current.reset(token)
                if observer.completion:
                    method, values = observer.completion
                    app.root.after(0, lambda: getattr(app, method)(*values))
        return wrapped
    return decorate


def task_metrics(**values):
    observer = _current.get()
    if observer:
        observer.summary.update({k: v for k, v in values.items()
                                 if isinstance(v, (int, float, bool))})


def select_log_dir(preferred, parameters):
    """Never place new diagnostic writes inside a directory being compared."""
    if not preferred:
        return None
    tasks = parameters.get("tasks") or [parameters]
    sources = []
    for task in tasks:
        keys = ("old_version", "new_version") if task.get("vcs_type") in (
            "folder", "archive") else ("project_path",)
        sources.extend(os.path.normcase(os.path.realpath(task[k]))
                       for k in keys if task.get(k))
    candidates = [Path(preferred),
        Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "CompareTool/logs/tasks",
        Path(tempfile.gettempdir()) / "CompareTool/logs/tasks"]
    for candidate in candidates:
        real = os.path.normcase(os.path.realpath(candidate))
        overlaps = False
        for source in sources:
            try:
                overlaps |= os.path.commonpath([real, source]) == source
            except ValueError:
                pass
        if not overlaps:
            return candidate
    return None
