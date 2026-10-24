"""主窗口的排除规则编辑、外部关联与路径试匹配。"""
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from exclusion_rules import (ENCODINGS, manual_snapshot, normalize_source,
                             read_rule_file, restore_snapshot)
from logger import warn


class ExclusionRulesUI:
    def _init_exclusion_settings(self, config, base_dir):
        self._rule_base_dir = base_dir
        self._project_exclude_sources = {}
        raw = config.get("project_exclude_sources", {})
        if not isinstance(raw, dict):
            message = "排除规则来源配置损坏，请重新选择手工规则或外部文件"
            warn(message)
            self._project_exclude_sources = {key: {"kind": "invalid", "error": message}
                                            for key in self._project_exclude_rules}
            raw = {}
        for key, value in raw.items():
            try:
                source = normalize_source(value, base_dir)
                if isinstance(value, dict) and "snapshot" in value:
                    source["snapshot"] = restore_snapshot(value["snapshot"]).to_dict()
                self._project_exclude_sources[key] = source
            except ValueError as exc:
                # 保留坏绑定并阻止生成；不能把“文件规则”静默变成手工默认。
                self._project_exclude_sources[key] = {"kind": "invalid", "error": str(exc)}
                warn(f"项目排除规则来源损坏：{key}：{exc}")
        recent = config.get("recent_exclude_files", [])
        self._recent_exclude_files = []
        if isinstance(recent, list):
            for value in recent:
                try:
                    source = normalize_source(value, base_dir)
                    if source["kind"] == "file" and source not in self._recent_exclude_files:
                        self._recent_exclude_files.append(source)
                except ValueError:
                    warn("忽略损坏的最近排除规则文件记录")
        self._recent_exclude_files = self._recent_exclude_files[:10]
        self._preview_rule_snapshot = None
        self._manual_exclude_draft = self._default_exclude_rules

    def _build_exclusion_controls(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X)
        self.exclude_source_var = tk.StringVar(value="manual")
        self.exclude_file_var = tk.StringVar()
        self.exclude_encoding_var = tk.StringVar(value="utf-8-sig")
        self.exclude_follow_var = tk.BooleanVar(value=True)
        self.exclude_status_var = tk.StringVar(value="来源：手工填写；每行一条 glob，不叠加其他规则")
        for label, value in (("手工填写", "manual"), ("关联外部文件", "file")):
            ttk.Radiobutton(bar, text=label, variable=self.exclude_source_var, value=value,
                            command=self._on_rule_source_changed).pack(side=tk.LEFT)
        ttk.Button(bar, text="选择文件", command=self._choose_rule_file).pack(side=tk.LEFT)
        encoding = ttk.Combobox(bar, textvariable=self.exclude_encoding_var,
                               values=ENCODINGS, state="readonly", width=10)
        encoding.pack(side=tk.LEFT, padx=4)
        encoding.bind("<<ComboboxSelected>>", lambda _event: self._reload_rule_preview())
        ttk.Button(bar, text="刷新预览", command=self._reload_rule_preview).pack(side=tk.LEFT)
        recent = ttk.Frame(parent)
        recent.pack(fill=tk.X, pady=(3, 0))
        ttk.Label(recent, text="文件 / 最近关联：").pack(side=tk.LEFT)
        self.exclude_file_combo = ttk.Combobox(recent, textvariable=self.exclude_file_var,
                                              state="readonly", width=35)
        self.exclude_file_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.exclude_file_combo.bind("<<ComboboxSelected>>", self._reuse_rule_file)
        editor = ttk.Frame(parent)
        editor.pack(fill=tk.X, pady=(3, 0))
        self.exclude_text = tk.Text(editor, height=4, wrap=tk.NONE)
        self.exclude_text.insert("1.0", self._default_exclude_rules)
        scroll = ttk.Scrollbar(editor, orient=tk.VERTICAL, command=self.exclude_text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.exclude_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.exclude_text.config(yscrollcommand=scroll.set)
        tools = ttk.Frame(parent)
        tools.pack(fill=tk.X, pady=(3, 0))
        self.exclude_template_var = tk.StringVar(value="源码比较")
        ttk.Combobox(tools, textvariable=self.exclude_template_var, state="readonly",
                     values=("源码比较", "部署包比较", "空白规则"), width=11).pack(side=tk.LEFT)
        ttk.Button(tools, text="应用为手工规则", command=self._apply_rule_template).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(tools, text="多项目每次生成跟随文件", variable=self.exclude_follow_var).pack(side=tk.LEFT)
        test = ttk.Frame(parent)
        test.pack(fill=tk.X, pady=(3, 0))
        ttk.Label(test, text="相对路径：").pack(side=tk.LEFT)
        self.exclude_test_var = tk.StringVar()
        ttk.Entry(test, textvariable=self.exclude_test_var, width=25).pack(side=tk.LEFT, padx=(0, 4), fill=tk.X, expand=True)
        ttk.Button(test, text="测试相对路径", command=self._test_rule_path).pack(side=tk.LEFT)
        status = ttk.Label(parent, textvariable=self.exclude_status_var, wraplength=700, foreground="#536577")
        status.pack(fill=tk.X, pady=(2, 0))
        parent.bind("<Configure>", lambda event: status.configure(wraplength=max(200, event.width - 4)))
        self._refresh_recent_rule_files()

    def _refresh_recent_rule_files(self):
        paths = list(dict.fromkeys(source["path"] for source in self._recent_exclude_files))
        self.exclude_file_combo.config(values=paths)

    def _current_rule_source(self):
        kind = self.exclude_source_var.get()
        if kind == "invalid":
            raise ValueError("保存的排除规则来源损坏，请重新选择手工规则或关联文件")
        return normalize_source(dict(kind=kind, path=self.exclude_file_var.get(),
                                     encoding=self.exclude_encoding_var.get(), follow=self.exclude_follow_var.get()),
                                self._rule_base_dir)

    def _remember_rule_file(self, source):
        self._recent_exclude_files = [source] + [item for item in self._recent_exclude_files
            if (item["path"], item["encoding"]) != (source["path"], source["encoding"])]
        self._recent_exclude_files = self._recent_exclude_files[:10]
        self._refresh_recent_rule_files()

    def _show_rule_snapshot(self, snapshot):
        self._preview_rule_snapshot = snapshot
        self._replace_exclude_text(snapshot.text)
        self.exclude_status_var.set(
            f"来源：{snapshot.path} | {snapshot.encoding} | {len(snapshot.patterns)} 条 | SHA-256 {snapshot.sha256[:12]}；"
            "# 整行注释；单项目生成时重读，不叠加默认规则")

    def _reload_rule_preview(self):
        if self.exclude_source_var.get() != "file":
            return
        try:
            source = self._current_rule_source()
            self._show_rule_snapshot(read_rule_file(source))
            self._remember_rule_file(source)
        except ValueError as exc:
            self._preview_rule_snapshot = None
            self._replace_exclude_text("")
            self.exclude_status_var.set(str(exc))
            messagebox.showerror("排除规则读取失败", str(exc))

    def _on_rule_source_changed(self):
        if self.exclude_source_var.get() == "manual":
            self._replace_exclude_text(self._manual_exclude_draft)
            self._preview_rule_snapshot = None
            self.exclude_status_var.set("来源：手工填写；每行一条 glob，不叠加其他规则")
        else:
            if self.exclude_text.cget("state") == tk.NORMAL:
                self._manual_exclude_draft = self.exclude_text.get("1.0", tk.END).strip()
            self.exclude_text.config(state=tk.DISABLED)
            if self.exclude_file_var.get():
                self._reload_rule_preview()
            else:
                self._replace_exclude_text("")
                self.exclude_status_var.set("请选择外部规则文件；也可在下拉框复用最近文件")

    def _choose_rule_file(self):
        path = filedialog.askopenfilename(title="关联排除规则文件", filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        self._link_rule_file(path)

    def _link_rule_file(self, path):
        if self.exclude_source_var.get() == "manual":
            self._manual_exclude_draft = self.exclude_text.get("1.0", tk.END).strip()
        self.exclude_file_var.set(path)
        self.exclude_source_var.set("file")
        self._reload_rule_preview()

    def _reuse_rule_file(self, _event=None):
        path = self.exclude_file_var.get()
        match = next((item for item in self._recent_exclude_files if item["path"] == path), None)
        if match:
            self.exclude_encoding_var.set(match["encoding"])
        self._link_rule_file(path)

    def _apply_rule_template(self):
        templates = {"源码比较": self._default_exclude_rules,
                     "部署包比较": "logs/**\ntmp/**\ntemp/**", "空白规则": ""}
        self._manual_exclude_draft = templates[self.exclude_template_var.get()]
        self.exclude_source_var.set("manual")
        self._on_rule_source_changed()

    def _current_rule_snapshot(self, *, for_task=False):
        # 兼容公共测试/集成中的无界面实例，保持旧手工入口。
        if not hasattr(self, "exclude_source_var"):
            return manual_snapshot(self._current_exclude_rules())
        source = self._current_rule_source()
        if source["kind"] == "manual":
            return manual_snapshot(self._current_exclude_rules())
        preview = self._preview_rule_snapshot
        if for_task and not source["follow"] and preview is not None:
            if (preview.path, preview.encoding) == (source["path"], source["encoding"]):
                return preview
        snapshot = read_rule_file(source)
        self._show_rule_snapshot(snapshot)
        self._remember_rule_file(source)
        return snapshot

    def _save_rule_binding(self, key):
        if not hasattr(self, "exclude_source_var"):
            return
        try:
            source = self._current_rule_source()
        except ValueError as exc:
            source = {"kind": "invalid", "error": str(exc)}
        if source["kind"] == "file" and self._preview_rule_snapshot is not None:
            source["snapshot"] = self._preview_rule_snapshot.to_dict()
        self._project_exclude_sources[key] = source

    def _cache_current_rule_text(self, key):
        if hasattr(self, "exclude_source_var") and self.exclude_source_var.get() == "invalid":
            return  # 界面中的错误占位空文本不能覆盖原来仍可恢复的规则。
        self._project_exclude_rules[key] = self._current_exclude_rules()

    def _restore_rule_binding(self, source, manual_text):
        if not hasattr(self, "exclude_source_var"):
            self._replace_exclude_text(manual_text)
            return
        self._manual_exclude_draft = manual_text
        self._preview_rule_snapshot = None
        if not source or source.get("kind") == "manual":
            self.exclude_source_var.set("manual")
            self._on_rule_source_changed()
            return
        try:
            binding = normalize_source(source, self._rule_base_dir)
            self.exclude_source_var.set("file")
            self.exclude_file_var.set(binding["path"])
            self.exclude_encoding_var.set(binding["encoding"])
            self.exclude_follow_var.set(binding["follow"])
            snapshot = restore_snapshot(source["snapshot"]) if "snapshot" in source else read_rule_file(binding)
            if (snapshot.kind, snapshot.path, snapshot.encoding) != ("file", binding["path"], binding["encoding"]):
                raise ValueError("排除规则快照与来源不一致，请重新关联")
            self._show_rule_snapshot(snapshot)
            self.exclude_status_var.set(self.exclude_status_var.get() + "（保存的预览；多项目固定任务使用此快照）")
        except ValueError as exc:
            self.exclude_source_var.set("invalid")
            self._replace_exclude_text("")
            self.exclude_status_var.set(str(exc) + "；请重新选择规则来源")

    def _test_rule_path(self):
        try:
            # 固定任务应试算保存的规则，不能偷偷用文件的新内容解释旧任务。
            snapshot = self._current_rule_snapshot(for_task=True)
            matches = snapshot.matches(self.exclude_test_var.get())
            source = snapshot.path or "手工填写"
            result = "排除" if matches else "保留（未命中排除规则）"
            detail = "\n".join(f"第 {number} 行：{pattern}" for number, pattern in matches)
            messagebox.showinfo("路径试匹配", f"结果：{result}\n来源：{source}\nSHA-256：{snapshot.sha256}\n{detail}\n\n按当前任务规则试匹配，不查询文件是否存在或发生变化。单项目生成仍会重读关联文件。")
        except ValueError as exc:
            messagebox.showerror("路径试匹配失败", str(exc))
