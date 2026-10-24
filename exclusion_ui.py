"""手工规则加一个可开关、按项目记忆的外置排除文件。"""
import tkinter as tk
from tkinter import ttk, filedialog

from exclusion_rules import combine_rules, manual_snapshot, normalize_source, read_rule_file
from logger import warn


class ExclusionRulesUI:
    def _init_exclusion_settings(self, config, base_dir):
        self._rule_base_dir = base_dir
        self._project_exclude_sources = {}
        raw = config.get("project_exclude_sources", {})
        if not isinstance(raw, dict):
            warn("排除文件配置损坏，请重新选择文件或取消勾选")
            self._project_exclude_sources = {key: {"kind": "invalid"}
                                            for key in self._project_exclude_rules}
            return
        for key, value in raw.items():
            try:
                self._project_exclude_sources[key] = normalize_source(value, base_dir)
            except ValueError as exc:
                self._project_exclude_sources[key] = {"kind": "invalid"}
                warn(f"项目排除文件配置损坏：{key}：{exc}")

    def _build_exclusion_controls(self, parent):
        self.exclude_file_var = tk.StringVar()
        self.exclude_enabled_var = tk.BooleanVar(value=False)
        self._invalid_rule_source = False
        editor = ttk.Frame(parent)
        editor.pack(fill=tk.X)
        self.exclude_text = tk.Text(editor, height=4, wrap=tk.NONE)
        self.exclude_text.insert("1.0", self._default_exclude_rules)
        scroll = ttk.Scrollbar(editor, orient=tk.VERTICAL, command=self.exclude_text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.exclude_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.exclude_text.config(yscrollcommand=scroll.set)
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(4, 0))
        ttk.Checkbutton(bar, text="使用外置排除文件", variable=self.exclude_enabled_var,
                        command=self._refresh_file_preview).pack(side=tk.LEFT)
        ttk.Button(bar, text="清空文件", command=self._clear_rule_file).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(bar, text="选择文件", command=self._choose_rule_file).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Entry(bar, textvariable=self.exclude_file_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        self._exclude_preview_frame = ttk.Frame(parent)
        self._exclude_preview_title = tk.StringVar()
        ttk.Label(self._exclude_preview_frame, textvariable=self._exclude_preview_title).pack(anchor=tk.W)
        preview = ttk.Frame(self._exclude_preview_frame)
        preview.pack(fill=tk.X)
        self.exclude_file_text = tk.Text(preview, height=3, wrap=tk.CHAR, state=tk.DISABLED)
        preview_scroll = ttk.Scrollbar(preview, orient=tk.VERTICAL, command=self.exclude_file_text.yview)
        preview_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.exclude_file_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.exclude_file_text.configure(yscrollcommand=preview_scroll.set)

    def _update_file_preview_title(self):
        state = "已启用" if self.exclude_enabled_var.get() else "未启用"
        self._exclude_preview_title.set(f"外置文件内容（只读，{state}）")

    def _show_file_preview(self, text):
        if not self.exclude_enabled_var.get() or not self.exclude_file_var.get():
            self._hide_file_preview()
            return
        self._exclude_preview_frame.pack(fill=tk.X, pady=(4, 0))
        self.exclude_file_text.configure(state=tk.NORMAL)
        self.exclude_file_text.delete("1.0", tk.END)
        # 只规范界面换行，报告中的原文和文件摘要继续保留实际读取内容。
        self.exclude_file_text.insert("1.0", text.replace("\r\n", "\n").replace("\r", "\n"))
        self.exclude_file_text.configure(state=tk.DISABLED)
        self._update_file_preview_title()

    def _hide_file_preview(self):
        if not hasattr(self, "_exclude_preview_frame"):
            return
        self._exclude_preview_frame.pack_forget()
        self.exclude_file_text.configure(state=tk.NORMAL)
        self.exclude_file_text.delete("1.0", tk.END)
        self.exclude_file_text.configure(state=tk.DISABLED)

    def _refresh_file_preview(self):
        path = self.exclude_file_var.get()
        if not self.exclude_enabled_var.get() or not path:
            self._hide_file_preview()
            return
        try:
            source = normalize_source(dict(kind="file", path=path, enabled=True, combine=True), self._rule_base_dir)
            self._show_file_preview(read_rule_file(source).text)
        except ValueError as exc:
            self._show_file_preview(str(exc))

    def _choose_rule_file(self):
        path = filedialog.askopenfilename(parent=self.root, title="选择外置排除文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if path:
            self._link_rule_file(path)

    def _link_rule_file(self, path):
        # 选择只记录路径；勾选后才读取、展示并参与排除。
        self.exclude_file_var.set(path)
        self._invalid_rule_source = False
        self._refresh_file_preview()

    def _clear_rule_file(self):
        self.exclude_enabled_var.set(False)
        self.exclude_file_var.set("")
        self._invalid_rule_source = False
        self._hide_file_preview()

    def _current_rule_source(self):
        if not hasattr(self, "exclude_enabled_var"):
            return {"kind": "manual"}
        enabled = self.exclude_enabled_var.get()
        if enabled and self._invalid_rule_source:
            raise ValueError("保存的排除文件配置损坏，请重新选择文件或取消勾选")
        path = self.exclude_file_var.get()
        if not enabled and not path:
            return {"kind": "manual"}
        return normalize_source(dict(kind="file", path=path, enabled=enabled, combine=True), self._rule_base_dir)

    def _current_rule_snapshot(self, *, for_task=False):
        text = self._current_exclude_rules()
        source = self._current_rule_source()
        if source["kind"] == "manual" or not source["enabled"]:
            self._hide_file_preview()
            return manual_snapshot(text)
        try:
            external = read_rule_file(source)
        except ValueError as exc:
            self._show_file_preview(str(exc))
            raise
        self._show_file_preview(external.text)
        return combine_rules(text, external)

    def _save_rule_binding(self, key):
        if not hasattr(self, "exclude_enabled_var"):
            return
        try:
            source = self._current_rule_source()
        except ValueError as exc:
            source = {"kind": "invalid"}
            warn(str(exc))
        self._project_exclude_sources[key] = source

    def _cache_current_rule_text(self, key):
        self._project_exclude_rules[key] = self._current_exclude_rules()

    def _restore_rule_binding(self, source, manual_text):
        if not hasattr(self, "exclude_enabled_var"):
            self._replace_exclude_text(manual_text)
            return
        # 上一版文件模式的文本框保存的是文件预览，不能转成永久手工规则。
        legacy_file = isinstance(source, dict) and source.get("kind") == "file" and not source.get("combine")
        self._replace_exclude_text("" if legacy_file else manual_text)
        self.exclude_file_var.set("")
        self.exclude_enabled_var.set(False)
        self._invalid_rule_source = False
        self._exclude_preview_frame.pack_forget()
        if not source or source.get("kind") == "manual":
            return
        try:
            binding = normalize_source(source, self._rule_base_dir)
            self.exclude_file_var.set(binding["path"])
            self.exclude_enabled_var.set(binding.get("enabled", True))
            self._refresh_file_preview()
        except ValueError:
            self._invalid_rule_source = True
            self.exclude_enabled_var.set(True)
