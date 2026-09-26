"""Tk widgets are accessed only by the UI thread's polling timer."""
import os
import sys
import time
from tkinter import messagebox
from app_version import build_info
from task_progress import ProgressMailbox


class TaskProgressUI:
    def _init_task_progress(self, config_dir):
        self._task_mailbox = ProgressMailbox()
        self._task_log_dir = os.path.join(config_dir, "logs", "tasks")
        self._task_after = None
        self._last_task_record = None
        self._task_started = 0.0
        self._task_bar_mode = "indeterminate"

    def _begin_task_progress(self):
        self._task_mailbox.publish(None)
        self._last_task_record = None
        self._task_started = time.perf_counter()
        self._task_bar_mode = "indeterminate"
        self.progress.configure(mode="indeterminate", maximum=100, value=0)
        self._task_after = self.root.after(200, self._poll_task_progress)

    def _poll_task_progress(self):
        self._task_after = None
        if not self._generating:
            return
        value = self._task_mailbox.latest() or {}
        elapsed = time.perf_counter() - value.get("started", self._task_started)
        done, total = value.get("done"), value.get("total")
        known = isinstance(total, int) and total > 0 and isinstance(done, int)
        mode = "determinate" if known else "indeterminate"
        if mode != self._task_bar_mode:
            self.progress.stop()
            self.progress.configure(mode=mode)
            if not known:
                self.progress.start()
            self._task_bar_mode = mode
        if known:
            self.progress.configure(maximum=total, value=min(done, total))
        label = value.get("label", "正在准备任务")
        count = f"：{done}/{total}" if known else ""
        prefix = value.get("project", "")
        text = f"{prefix} {label}{count}　已用 {elapsed:.1f} 秒".strip()
        if value.get("log_error"):
            text += "（耗时日志不可写）"
        self.status_var.set(text)
        self._task_after = self.root.after(200, self._poll_task_progress)

    def _end_task_progress(self):
        if self._task_after is not None:
            self.root.after_cancel(self._task_after)
            self._task_after = None
        self.progress.stop()
        self.progress.configure(mode="indeterminate", maximum=100, value=0)

    def _task_status_suffix(self):
        data = getattr(self, "_last_task_record", None)
        return f"　耗时 {data['seconds']:.1f} 秒" if isinstance(data, dict) else ""

    def _task_details_note(self):
        data = getattr(self, "_last_task_record", None)
        if not isinstance(data, dict):
            return ""
        text = f"\n总耗时：{data['seconds']:.1f} 秒\n"
        if data.get("log_error"):
            return text + "耗时日志未完整保存，比较结果不受影响。\n"
        if data.get("log_path"):
            text += f"耗时日志：\n{data['log_path']}\n"
        return text

    def _show_version_info(self):
        data = build_info()
        origin = data.get("commit") or "源码运行（未打包）"
        if data.get("commit") and data.get("dirty"):
            origin += "（构建时含未提交修改）"
        path = sys.executable if getattr(sys, "frozen", False) else os.path.join(os.path.dirname(os.path.dirname(self._task_log_dir)), "main.py")
        text = (f"版本：{data['version']}\n构建：{data['build_id']}\n"
                f"构建时间：{data['built_at']}\n源码基线：{origin}\n"
                f"源码指纹：{data.get('source_sha256', '源码模式无构建指纹')}\n\n"
                f"运行文件：\n{path}\n\n耗时日志目录：\n{self._task_log_dir}")
        messagebox.showinfo("版本与诊断信息", text, parent=self.root)
