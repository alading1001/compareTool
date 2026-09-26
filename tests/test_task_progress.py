import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import task_progress as tp
from main import CompareToolApp
from ui_progress import TaskProgressUI


class TaskProgressTests(unittest.TestCase):
    def setUp(self):
        Path(".tmp").mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.observer = tp.TaskObserver("folder", log_dir=self.root / "logs")
        token = tp._current.set(self.observer)
        self.addCleanup(tp._current.reset, token)
        self.addCleanup(lambda: self.observer.stream and self.observer.stream.close())

    def test_phase_counts_and_exception_propagation(self):
        with self.assertRaises(ValueError), tp.stage("outer", "外层"):
            with tp.stage("inner", "内层"):
                tp.progress(2, 3)
                raise ValueError("original")
        self.assertEqual([], self.observer.stack)
        self.assertEqual(1, self.observer.phases["inner"]["calls"])
        outer = self.observer.phases["outer"]
        inner = self.observer.phases["inner"]
        self.assertAlmostEqual(outer["seconds"] - inner["seconds"], outer["exclusive_seconds"])

    def test_no_active_observer_is_a_noop(self):
        token = tp._current.set(None)
        try:
            with tp.stage("noop", "nothing"):
                tp.progress(1, 1)
                tp.advance()
        finally:
            tp._current.reset(token)
        self.assertFalse(self.observer.phases)

    def test_mailbox_is_bounded_and_throttled(self):
        self.observer.clock = lambda: 1.0
        with tp.stage("x", "x"):
            with mock.patch.object(self.observer.mailbox, "publish", wraps=self.observer.mailbox.publish) as publish:
                for i in range(10000):
                    tp.progress(i, 10000)
                self.assertEqual(0, publish.call_count)
                self.observer.clock = lambda: 1.3
                tp.progress(10000, 10000)
                self.assertEqual(1, publish.call_count)
                self.assertEqual(10000, self.observer.mailbox.latest()["done"])

    def test_thread_context_isolation(self):
        results = []
        def worker():
            results.append(tp._current.get())
            with tp.stage("other", "other"):
                tp.progress(1, 2)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual([None], results)
        self.assertFalse(self.observer.phases)

    def test_unwritable_log_does_not_abort(self):
        blocked = self.root / "file"
        blocked.write_text("keep", encoding="utf-8")
        obs = tp.TaskObserver("folder", log_dir=blocked / "logs")
        obs.completion = "_on_complete", ("report", {})
        result = obs.finish()
        self.assertTrue(result["success"])
        self.assertTrue(result["log_error"])
        self.assertEqual("keep", blocked.read_text())

    def test_log_has_identity_counts_and_terminal_record(self):
        tp.task_metrics(overwrite_targets=4)
        tp.defer_completion(None, "_on_complete", "report", {"total_files": 3})
        result = self.observer.finish()
        self.assertEqual(4, result["counts"]["overwrite_targets"])
        self.assertEqual(3, result["counts"]["total_files"])
        rows = [json.loads(s) for s in Path(result["log_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual("task_start", rows[0]["event"])
        self.assertEqual("task_end", rows[-1]["event"])
        self.assertIn("build_id", rows[0]["build"])
        self.assertTrue(Path(result["log_path"]).with_suffix(".json").exists())

    def test_log_limit_keeps_final_result(self):
        self.observer.MAX_EVENT_BYTES = 1
        self.observer.completion = "_on_complete", ("report", {})
        self.observer.record("ignored")
        result = self.observer.finish()
        rows = Path(result["log_path"]).read_text(encoding="utf-8")
        self.assertNotIn('"event": "ignored"', rows)
        self.assertIn('"event": "task_end"', rows)

    def test_completion_waits_for_cleanup_and_record(self):
        events = []
        app = SimpleNamespace(root=mock.Mock(), _task_log_dir=self.root / "job")
        @tp.observe_job("single")
        def work(app):
            tp.defer_completion(app, "_on_complete", "report", {"total_files": 2})
            self.assertEqual(0, app.root.after.call_count)
            events.append("cleaned")
        work(app)
        self.assertTrue(app._last_task_record["success"])
        self.assertEqual(["cleaned"], events)
        self.assertEqual(1, app.root.after.call_count)
        self.assertIs(tp._current.get(), self.observer)

    def test_cleanup_failure_cannot_report_success(self):
        app = SimpleNamespace(root=mock.Mock())
        @tp.observe_job("single")
        def work(app):
            tp.defer_completion(app, "_on_complete", "report", {})
            raise RuntimeError("cleanup failed")
        work(app)
        self.assertFalse(app._last_task_record["success"])
        self.assertEqual("RuntimeError", app._last_task_record["error_type"])

    def test_ui_poll_stops_after_completion(self):
        ui = TaskProgressUI()
        ui.root = mock.Mock()
        ui.progress = mock.Mock()
        ui.status_var = mock.Mock()
        ui._init_task_progress(str(self.root))
        ui._generating = True
        ui._begin_task_progress()
        ui._generating = False
        ui._end_task_progress()
        ui._poll_task_progress()
        ui.status_var.set.assert_not_called()
        self.assertEqual(1, ui.root.after.call_count)

    def test_real_folder_generation_and_overwrite_logged(self):
        old, new, output = [self.root / n for n in ("old", "new", "output")]
        for p in (old, new, output):
            p.mkdir()
        (old / "a.txt").write_bytes(b"old\n")
        (new / "a.txt").write_bytes(b"new\n")
        app = CompareToolApp.__new__(CompareToolApp)
        app.root = mock.Mock()
        app._task_log_dir = self.root / "jobs"
        with mock.patch.dict("os.environ", {
                "COMPARETOOL_TEMP_DIR": str(self.root / "runtime"),
                "COMPARETOOL_TRANSACTION_KEY_FILE": str(self.root / "key")}):
            for expected in (0, 4):
                app._do_generate(str(new), "folder", str(old), str(new),
                    "Demo", [], True, True, str(output / "report.html"),
                    str(output / "oldVersion"), str(output / "newVersion"), str(output))
                result = app._last_task_record
                self.assertTrue(result["success"], result)
                self.assertEqual(expected, result["counts"]["overwrite_targets"])
                self.assertEqual(1, result["counts"]["total_files"])
                self.assertIn("folder.compare", result["phases"])
                self.assertIn("output.commit", result["phases"])
                self.assertEqual(b"new\n", (output / "newVersion/Demo/a.txt").read_bytes())


if __name__ == "__main__":
    unittest.main()
