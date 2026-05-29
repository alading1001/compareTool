import json
import os
import sys
import threading
import webbrowser
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox

# PyInstaller 打包后的资源路径处理
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    CONFIG_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = BASE_DIR

CONFIG_FILE = os.path.join(CONFIG_DIR, "compareTool_config.json")

DEFAULT_EXCLUDE_RULES = "\n".join([
    "**/.git/**",
    "**/.svn/**",
    "",
    "*.class",
    "*.war",
    "*.ear",
    "**/target/**",
    "**/build/**",
    "**/bin/**",
    "**/dist/**",
    "**/out/**",
    "**/.gradle/**",
    "",
    "**/node_modules/**",
    "**/__pycache__/**",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.tox/**",
    "**/.venv/**",
    "**/venv/**",
    "",
    "**/logs/**",
    "*.log",
    "**/tmp/**",
    "**/temp/**",
    "**/coverage/**",
    "**/htmlcov/**",
    "",
    "**/.idea/**",
    "**/.settings/**",
    "**/.vscode/**",
    ".project",
    ".classpath",
    "*.iml",
    "",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
]).strip()

from vcs.git_vcs import GitVCS
from vcs.svn_vcs import SVNVCS
from vcs.folder_vcs import FolderVCS
from vcs.archive_vcs import ArchiveVCS
from vcs.multi_version_vcs import GitMultiVersionVCS, SVNMultiVersionVCS, parse_multi_versions
from diff_engine import DiffEngine
from report_generator import ReportGenerator
from file_exporter import FileExporter
from logger import info, warn, error


def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class CompareToolApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("代码比对报告工具")
        self.root.resizable(True, True)
        self.root.minsize(600, 700)
        # 窗口居中显示
        w, h = 760, 960
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws - w) // 2
        y = 0
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            self.root.iconbitmap(icon_path)

        self._default_output = os.path.join(os.path.expanduser("~"), "Desktop")
        self._update_after_id = None
        self._config = _load_config()
        self._default_exclude_rules = self._load_default_exclude_rules()
        self._project_exclude_rules = dict(self._config.get("project_exclude_rules", {}))
        self._multi_tasks = list(self._config.get("multi_tasks", []))
        self._editing_task_index = None
        self._last_exclude_key = ""
        self._project_name_manual = False
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ========== UI 构建 ==========

    def _load_default_exclude_rules(self) -> str:
        """读取内置默认排除规则模板。项目专属规则由 project_exclude_rules 负责。"""
        return DEFAULT_EXCLUDE_RULES

    def _build_ui(self):
        container = ttk.Frame(self.root)
        container.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        main = ttk.Frame(canvas, padding="16")
        window_id = canvas.create_window((0, 0), window=main, anchor="nw")

        def _sync_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_frame_width(event):
            canvas.itemconfigure(window_id, width=event.width)

        main.bind("<Configure>", _sync_scroll_region)
        canvas.bind("<Configure>", _sync_frame_width)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # ── 项目目录 ──
        self.project_label = ttk.Label(main, text="项目目录:", font=("", 10))
        self.project_label.grid(row=0, column=0, sticky=tk.W, pady=(0, 2))
        self.project_dir_frame = ttk.Frame(main)
        self.project_dir_frame.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        self.dir_entry = ttk.Entry(self.project_dir_frame)
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.dir_entry.bind("<KeyRelease>", lambda e: self._on_project_path_changed())
        self.dir_entry.bind("<FocusOut>", lambda e: self._on_project_path_changed())
        ttk.Button(self.project_dir_frame, text="浏览...", command=self._browse_project).pack(side=tk.LEFT, padx=(6, 0))
        # 恢复上次项目路径
        last_project = self._config.get("project_path", "")
        if last_project:
            self.dir_entry.insert(0, last_project)

        # ── VCS 类型 ──
        ttk.Label(main, text="版本控制类型:", font=("", 10)).grid(row=2, column=0, sticky=tk.W, pady=(0, 4))
        vcs_frame = ttk.Frame(main)
        vcs_frame.grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))
        self.vcs_var = tk.StringVar(value=self._config.get("vcs_type", "git"))
        self.vcs_var.trace_add("write", lambda *_: self._on_vcs_changed())
        ttk.Radiobutton(vcs_frame, text="Git", variable=self.vcs_var, value="git").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_frame, text="SVN", variable=self.vcs_var, value="svn").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_frame, text="文件夹", variable=self.vcs_var, value="folder").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_frame, text="压缩包", variable=self.vcs_var, value="archive").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_frame, text="Git需求包", variable=self.vcs_var, value="git_multi").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_frame, text="SVN需求包", variable=self.vcs_var, value="svn_multi").pack(side=tk.LEFT)

        # ── 报告项目名 ──
        ttk.Label(main, text="项目名 (报告树/变更清单/导出目录使用):", font=("", 10)).grid(row=4, column=0, sticky=tk.W, pady=(0, 4))
        project_name_frame = ttk.Frame(main)
        project_name_frame.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        self.project_name_var = tk.StringVar()
        self.project_name_entry = ttk.Entry(project_name_frame, textvariable=self.project_name_var)
        self.project_name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.project_name_entry.bind("<KeyRelease>", lambda e: setattr(self, "_project_name_manual", True))

        # ── 排除规则 ──
        ttk.Label(main, text="排除规则 (每行一个，支持 * 和 ** 通配符):", font=("", 10)).grid(row=6, column=0, sticky=tk.W, pady=(0, 4))
        exclude_frame = ttk.Frame(main)
        exclude_frame.grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        self.exclude_text = tk.Text(exclude_frame, height=4, wrap=tk.NONE)
        self.exclude_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.exclude_text.insert("1.0", self._default_exclude_rules)
        exclude_scroll = ttk.Scrollbar(exclude_frame, orient=tk.VERTICAL, command=self.exclude_text.yview)
        exclude_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.exclude_text.config(yscrollcommand=exclude_scroll.set)

        # ── 版本 / 文件夹选择 ──
        self.old_version_var = tk.StringVar()
        self.new_version_var = tk.StringVar()
        self._new_version_cb_id = self.new_version_var.trace_add("write", lambda *_: self._update_output_paths())

        # 旧版本标签（动态切换）
        self.old_label = ttk.Label(main, text="旧版本 (改动前):", font=("", 10))
        self.old_label.grid(row=8, column=0, sticky=tk.W, pady=(0, 2))

        old_frame = ttk.Frame(main)
        self.old_version_frame = old_frame
        old_frame.grid(row=9, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self.old_entry = ttk.Entry(old_frame, textvariable=self.old_version_var)
        self.old_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.old_vcs_btn = ttk.Button(old_frame, text="获取版本列表", command=lambda: self._fetch_versions("old"))
        self.old_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.old_folder_btn = ttk.Button(old_frame, text="浏览...", command=lambda: self._browse_dir(self.old_version_var))
        self.old_archive_btn = ttk.Button(old_frame, text="选择压缩包...", command=lambda: self._browse_archive(self.old_version_var))
        # 文件夹/压缩包浏览按钮初始隐藏

        # 新版本标签（动态切换）
        self.new_label = ttk.Label(main, text="新版本 (改动后):", font=("", 10))
        self.new_label.grid(row=10, column=0, sticky=tk.W, pady=(0, 2))

        new_frame = ttk.Frame(main)
        self.new_version_frame = new_frame
        new_frame.grid(row=11, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self.new_entry = ttk.Entry(new_frame, textvariable=self.new_version_var)
        self.new_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.new_vcs_btn = ttk.Button(new_frame, text="获取版本列表", command=lambda: self._fetch_versions("new"))
        self.new_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.new_folder_btn = ttk.Button(new_frame, text="浏览...", command=lambda: self._browse_dir(self.new_version_var))
        self.new_archive_btn = ttk.Button(new_frame, text="选择压缩包...", command=lambda: self._browse_archive(self.new_version_var))
        # 文件夹/压缩包浏览按钮初始隐藏

        # 版本列表 + 填入按钮（仅 Git/SVN 模式使用）
        self.version_listbox = tk.Listbox(main, height=7, exportselection=False, selectmode=tk.SINGLE)
        self.version_listbox.grid(row=12, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        self.version_listbox.grid_remove()
        self.version_listbox.bind("<ButtonRelease-1>", self._on_version_click)

        fill_btn_frame = ttk.Frame(main)
        fill_btn_frame.grid(row=13, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        fill_btn_frame.grid_remove()
        self.fill_btn_frame = fill_btn_frame
        self.fill_target_label = ttk.Label(fill_btn_frame, text="", font=("", 9))
        self.fill_target_label.pack(side=tk.LEFT)
        self.fill_selected_btn = ttk.Button(fill_btn_frame, text="← 填入选中版本", command=self._fill_selected_version)
        self.fill_selected_btn.pack(side=tk.RIGHT)

        self._version_target = "old"

        # ── 输出路径 ──
        ttk.Label(main, text="输出路径设置:", font=("", 10, "bold")).grid(row=14, column=0, sticky=tk.W, pady=(10, 4))

        ttk.Label(main, text="输出目录:").grid(row=15, column=0, sticky=tk.W)
        output_dir_frame = ttk.Frame(main)
        output_dir_frame.grid(row=16, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.output_dir_var = tk.StringVar(value=self._config.get("output_dir", ""))
        self.output_dir_var.trace_add("write", lambda *_: self._update_output_paths())
        ttk.Entry(output_dir_frame, textvariable=self.output_dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(output_dir_frame, text="浏览...", command=lambda: self._browse_dir(self.output_dir_var)).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(main, text="比对报告保存到 (自动生成):").grid(row=17, column=0, sticky=tk.W)
        report_frame = ttk.Frame(main)
        report_frame.grid(row=18, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.report_path_var = tk.StringVar()
        ttk.Entry(report_frame, textvariable=self.report_path_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(main, text="旧版本变更文件导出到 (自动生成):").grid(row=19, column=0, sticky=tk.W)
        old_export_frame = ttk.Frame(main)
        old_export_frame.grid(row=20, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.old_export_var = tk.StringVar()
        ttk.Entry(old_export_frame, textvariable=self.old_export_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(main, text="新版本变更文件导出到 (自动生成):").grid(row=21, column=0, sticky=tk.W)
        new_export_frame = ttk.Frame(main)
        new_export_frame.grid(row=22, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        self.new_export_var = tk.StringVar()
        ttk.Entry(new_export_frame, textvariable=self.new_export_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── 显示选项 ──
        ttk.Label(main, text="显示选项:", font=("", 10, "bold")).grid(row=23, column=0, sticky=tk.W, pady=(10, 4))
        show_root_frame = ttk.Frame(main)
        show_root_frame.grid(row=24, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        ttk.Label(show_root_frame, text="报告树及变更清单使用项目名:").pack(side=tk.LEFT)
        self.show_project_root_var = tk.StringVar(value="yes")
        ttk.Radiobutton(show_root_frame, text="是", variable=self.show_project_root_var, value="yes").pack(side=tk.LEFT, padx=(6, 2))
        ttk.Radiobutton(show_root_frame, text="否", variable=self.show_project_root_var, value="no").pack(side=tk.LEFT)

        show_ctx_frame = ttk.Frame(main)
        show_ctx_frame.grid(row=25, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        ttk.Label(show_ctx_frame, text="差异展示方式:").pack(side=tk.LEFT)
        self.show_full_context_var = tk.StringVar(value="yes")
        ttk.Radiobutton(show_ctx_frame, text="全部内容", variable=self.show_full_context_var, value="yes").pack(side=tk.LEFT, padx=(6, 2))
        ttk.Radiobutton(show_ctx_frame, text="仅差异上下文", variable=self.show_full_context_var, value="no").pack(side=tk.LEFT)

        # ── SVN 设置 (仅在 SVN 模式显示) ──
        self.svn_path_label = ttk.Label(main, text="SVN 可执行文件路径 (可选，留空使用系统默认):", font=("", 10, "bold"))
        self.svn_path_label.grid(row=26, column=0, sticky=tk.W, pady=(10, 4))
        self.svn_path_frame = ttk.Frame(main)
        self.svn_path_frame.grid(row=27, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.svn_path_var = tk.StringVar(value=self._config.get("svn_path", ""))
        ttk.Entry(self.svn_path_frame, textvariable=self.svn_path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(self.svn_path_frame, text="浏览...", command=lambda: self._browse_svn_path()).pack(side=tk.LEFT, padx=(6, 0))

        # ── 多项目批量任务 ──
        ttk.Label(main, text="多项目批量任务:", font=("", 10, "bold")).grid(row=28, column=0, sticky=tk.W, pady=(10, 4))
        multi_btn_frame = ttk.Frame(main)
        multi_btn_frame.grid(row=29, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        self.add_task_btn = ttk.Button(multi_btn_frame, text="添加到多项目任务", command=self._add_or_update_multi_task)
        self.add_task_btn.pack(side=tk.LEFT)
        self.cancel_edit_btn = ttk.Button(multi_btn_frame, text="取消编辑", command=self._cancel_edit_task, state=tk.DISABLED)
        self.cancel_edit_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(multi_btn_frame, text="编辑任务", command=self._edit_multi_task).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(multi_btn_frame, text="删除任务", command=self._delete_multi_task).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(multi_btn_frame, text="清空任务", command=self._clear_multi_tasks).pack(side=tk.LEFT, padx=(6, 0))
        self.generate_multi_btn = ttk.Button(multi_btn_frame, text="生成多项目总报告", command=self._generate_multi)
        self.generate_multi_btn.pack(side=tk.RIGHT)

        multi_list_frame = ttk.Frame(main)
        multi_list_frame.grid(row=30, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self.multi_task_tree = ttk.Treeview(
            multi_list_frame,
            columns=("name", "type", "source", "versions"),
            show="headings",
            height=4
        )
        self.multi_task_tree.heading("name", text="项目名")
        self.multi_task_tree.heading("type", text="类型")
        self.multi_task_tree.heading("source", text="项目路径/来源")
        self.multi_task_tree.heading("versions", text="版本")
        self.multi_task_tree.column("name", width=120, stretch=False)
        self.multi_task_tree.column("type", width=90, stretch=False)
        self.multi_task_tree.column("source", width=260, stretch=True)
        self.multi_task_tree.column("versions", width=220, stretch=True)
        self.multi_task_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        multi_scroll = ttk.Scrollbar(multi_list_frame, orient=tk.VERTICAL, command=self.multi_task_tree.yview)
        multi_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.multi_task_tree.config(yscrollcommand=multi_scroll.set)

        # ── 底部 ──
        bottom_frame = ttk.Frame(main)
        bottom_frame.grid(row=31, column=0, columnspan=3, sticky=tk.EW, pady=(6, 0))

        self.progress = ttk.Progressbar(bottom_frame, mode="indeterminate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))

        self.generate_btn = ttk.Button(bottom_frame, text="生成比对报告", command=self._generate)
        self.generate_btn.pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom_frame, textvariable=self.status_var, font=("", 9)).pack(side=tk.RIGHT, padx=(0, 16))

        main.columnconfigure(0, weight=1)

        # 初始化输出路径和 VCS UI
        self._render_multi_tasks()
        self._on_vcs_changed()
        self._last_project_path = self._normalize_project_path(self.dir_entry.get().strip())
        self._refresh_project_name_default(force=True)
        self._switch_exclude_rules_for_current_source(save_previous=False)

    # ========== 界面交互 ==========

    @staticmethod
    def _normalize_project_path(path: str) -> str:
        if not path:
            return ""
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))

    def _is_project_vcs_mode(self) -> bool:
        return self.vcs_var.get() in ("git", "svn", "git_multi", "svn_multi")

    @staticmethod
    def _vcs_label(vcs_type: str) -> str:
        return {
            "git": "Git",
            "svn": "SVN",
            "folder": "文件夹",
            "archive": "压缩包",
            "git_multi": "Git需求包",
            "svn_multi": "SVN需求包",
        }.get(vcs_type, vcs_type)

    @staticmethod
    def _sanitize_project_name(name: str) -> str:
        cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in (name or "").strip())
        return cleaned.strip(" .") or "project"

    def _replace_exclude_text(self, text: str):
        self.exclude_text.delete("1.0", tk.END)
        self.exclude_text.insert("1.0", text or "")

    def _current_exclude_rules(self) -> str:
        return self.exclude_text.get("1.0", tk.END).strip()

    def _project_key_for_values(self, vcs_type: str, project_path: str, old_version: str, new_version: str) -> str:
        if vcs_type == "archive":
            source = new_version
            if source and not os.path.isfile(source):
                return ""
        elif vcs_type == "folder":
            source = new_version
            if source and not os.path.isdir(source):
                return ""
        else:
            source = project_path
            if source and not os.path.isdir(source):
                return ""
        return self._normalize_project_path(source) if source else ""

    def _current_project_key(self) -> str:
        return self._project_key_for_values(
            self.vcs_var.get(),
            self.dir_entry.get().strip(),
            self.old_version_var.get().strip(),
            self.new_version_var.get().strip(),
        )

    def _save_current_exclude_rules_for_current_key(self):
        key = self._current_project_key()
        if key:
            self._project_exclude_rules[key] = self._current_exclude_rules()

    def _switch_exclude_rules_for_current_source(self, save_previous: bool = True):
        key = self._current_project_key()
        old_key = getattr(self, "_last_exclude_key", "")
        if key == old_key:
            return
        if save_previous and old_key:
            self._project_exclude_rules[old_key] = self._current_exclude_rules()
        rules = self._project_exclude_rules.get(key, self._default_exclude_rules) if key else self._default_exclude_rules
        self._replace_exclude_text(rules)
        self._last_exclude_key = key

    def _default_project_name(self) -> str:
        vcs_type = self.vcs_var.get()
        project_path = self.dir_entry.get().strip()
        old_version = self.old_version_var.get().strip()
        new_version = self.new_version_var.get().strip()
        if vcs_type == "archive":
            source = new_version
            name = os.path.basename(source)
            for suffix in (".tar.gz", ".tar.bz2"):
                if name.lower().endswith(suffix):
                    return name[:-len(suffix)] or "project"
            return os.path.splitext(name)[0] if name else "project"
        if vcs_type == "folder":
            source = new_version or old_version
            return os.path.basename(os.path.normpath(source)) if source else "project"
        return os.path.basename(os.path.normpath(project_path)) if project_path else "project"

    def _refresh_project_name_default(self, force: bool = False):
        if force or not self._project_name_manual or not self.project_name_var.get().strip():
            self.project_name_var.set(self._sanitize_project_name(self._default_project_name()))

    def _on_project_path_changed(self):
        """项目路径变化时，清空 Git/SVN 相关版本选择，避免跨项目复用版本号。"""
        current = self._normalize_project_path(self.dir_entry.get().strip())
        previous = getattr(self, "_last_project_path", current)
        if current != previous:
            self._last_project_path = current
            self._project_name_manual = False
            if self._is_project_vcs_mode():
                self.old_version_var.set("")
                self.new_version_var.set("")
                self.version_listbox.delete(0, tk.END)
                self.version_listbox.grid_remove()
                self.fill_btn_frame.grid_remove()
                self.status_var.set("项目路径已变更，请重新选择版本")
            self._refresh_project_name_default(force=True)
            self._switch_exclude_rules_for_current_source()
        self._update_output_paths()

    def _update_output_paths(self, *_):
        """防抖：合并高频调用，延迟 50ms 执行"""
        if not hasattr(self, "output_dir_var"):
            return
        if self._update_after_id:
            self.root.after_cancel(self._update_after_id)
        self._update_after_id = self.root.after(50, self._do_update_output_paths)

    def _do_update_output_paths(self):
        """根据输出目录和项目名自动计算三条路径"""
        self._update_after_id = None
        self._refresh_project_name_default()
        self._switch_exclude_rules_for_current_source()
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            self.report_path_var.set("")
            self.old_export_var.set("")
            self.new_export_var.set("")
            return

        project_name = self._sanitize_project_name(
            self.project_name_var.get().strip() or self._default_project_name()
        )

        self.report_path_var.set(os.path.join(output_dir, f"{project_name}_diff.html"))
        self.old_export_var.set(os.path.join(output_dir, "oldVersion"))
        self.new_export_var.set(os.path.join(output_dir, "newVersion"))

    def _on_vcs_changed(self):
        """VCS 类型切换时更新界面"""
        vcs_type = self.vcs_var.get()
        is_folder = vcs_type == "folder"
        is_archive = vcs_type == "archive"
        is_multi = vcs_type in ("git_multi", "svn_multi")
        self.status_var.set("就绪")
        self._project_name_manual = False

        # 临时解绑 trace，避免 set("") 触发 _update_output_paths 中间态
        try:
            cb_name = self.new_version_var.trace_remove("write", self._new_version_cb_id)
        except (AttributeError, tk.TclError):
            pass

        self.old_entry.config(state=tk.NORMAL)
        self.new_entry.config(state=tk.NORMAL)
        self.old_version_var.set("")
        self.new_version_var.set("")
        self.version_listbox.config(selectmode=tk.SINGLE)
        self.fill_selected_btn.config(text="← 填入选中版本")

        if is_folder:
            self.project_label.grid_remove()
            self.project_dir_frame.grid_remove()
            self.svn_path_label.grid_remove()
            self.svn_path_frame.grid_remove()
            self.old_label.config(text="旧版本文件夹:")
            self.new_label.config(text="新版本文件夹:")
            self.old_vcs_btn.pack_forget()
            self.old_archive_btn.pack_forget()
            self.old_folder_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.new_vcs_btn.pack_forget()
            self.new_archive_btn.pack_forget()
            self.new_folder_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.version_listbox.grid_remove()
            self.fill_btn_frame.grid_remove()
            self.report_path_var.set("")
        elif is_archive:
            self.project_label.grid_remove()
            self.project_dir_frame.grid_remove()
            self.svn_path_label.grid_remove()
            self.svn_path_frame.grid_remove()
            self.old_label.config(text="旧版本压缩包:")
            self.new_label.config(text="新版本压缩包:")
            self.old_vcs_btn.pack_forget()
            self.old_folder_btn.pack_forget()
            self.old_archive_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.new_vcs_btn.pack_forget()
            self.new_folder_btn.pack_forget()
            self.new_archive_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.version_listbox.grid_remove()
            self.fill_btn_frame.grid_remove()
            self.report_path_var.set("")
        elif is_multi:
            self.project_label.grid()
            self.project_dir_frame.grid()
            is_svn_multi = vcs_type == "svn_multi"
            if is_svn_multi:
                self.svn_path_label.grid()
                self.svn_path_frame.grid()
            else:
                self.svn_path_label.grid_remove()
                self.svn_path_frame.grid_remove()
            self.old_label.config(text="选择需求版本 (可多选):")
            self.new_label.config(text="生成结果:")
            self.old_folder_btn.pack_forget()
            self.old_archive_btn.pack_forget()
            self.old_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.new_folder_btn.pack_forget()
            self.new_archive_btn.pack_forget()
            self.new_vcs_btn.pack_forget()
            self.new_version_var.set("基线 + 选中版本")
            self.new_entry.config(state="readonly")
            self.version_listbox.config(selectmode=tk.EXTENDED)
            self.fill_selected_btn.config(text="← 填入选中版本")
            self.version_listbox.grid_remove()
            self.fill_btn_frame.grid_remove()
            self._update_output_paths()
        else:
            self.project_label.grid()
            self.project_dir_frame.grid()
            is_svn = vcs_type == "svn"
            if is_svn:
                self.svn_path_label.grid()
                self.svn_path_frame.grid()
            else:
                self.svn_path_label.grid_remove()
                self.svn_path_frame.grid_remove()
            self.old_label.config(text="旧版本 (改动前):")
            self.new_label.config(text="新版本 (改动后):")
            self.old_folder_btn.pack_forget()
            self.old_archive_btn.pack_forget()
            self.old_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.new_folder_btn.pack_forget()
            self.new_archive_btn.pack_forget()
            self.new_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.version_listbox.grid_remove()
            self.fill_btn_frame.grid_remove()
            # 切回 Git/SVN，按项目名重新计算路径
            self._update_output_paths()

        # 重新绑定 trace
        self._new_version_cb_id = self.new_version_var.trace_add("write", lambda *_: self._update_output_paths())
        self._refresh_project_name_default(force=True)
        self._switch_exclude_rules_for_current_source()

    def _browse_project(self):
        path = filedialog.askdirectory(title="选择项目目录")
        if path:
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, path)
            self._on_project_path_changed()

    def _browse_dir(self, var):
        path = filedialog.askdirectory(title="选择保存目录")
        if path:
            var.set(path)

    def _browse_archive(self, var):
        path = filedialog.askopenfilename(
            title="选择压缩包",
            filetypes=[
                ("压缩文件", "*.zip *.jar *.war *.ear *.aar *.tar *.tar.gz *.tgz *.tar.bz2 *.tbz2"),
                ("所有文件", "*.*"),
            ]
        )
        if path:
            var.set(path)

    def _browse_svn_path(self):
        path = filedialog.askopenfilename(
            title="选择 SVN 可执行文件 (svn.exe)",
            filetypes=[("SVN 可执行文件", "svn.exe"), ("所有文件", "*.*")]
        )
        if path:
            self.svn_path_var.set(path)

    def _browse_save_file(self, var, desc, ext):
        path = filedialog.asksaveasfilename(title=f"保存{desc}", filetypes=[(desc, ext)], defaultextension=ext)
        if path:
            var.set(path)

    # ========== 版本列表获取 ==========

    def _fetch_versions(self, target="old"):
        self._version_target = target

        vcs_type = self.vcs_var.get()
        if vcs_type == "archive":
            messagebox.showinfo("提示", "压缩包模式无需获取版本列表，请直接选择压缩包文件。")
            return
        if vcs_type == "folder":
            messagebox.showinfo("提示", "文件夹模式无需获取版本列表，请直接选择文件夹。")
            return

        project_path = self.dir_entry.get().strip()
        if not project_path:
            messagebox.showwarning("提示", "请先选择项目目录")
            return

        self.version_listbox.delete(0, tk.END)
        self.version_listbox.insert(tk.END, "正在获取版本列表，请稍候...")
        self.version_listbox.grid()
        self.fill_btn_frame.grid()
        is_multi = vcs_type in ("git_multi", "svn_multi")
        self.fill_target_label.config(
            text="将填入: " + ("需求版本列表" if is_multi else ("旧版本" if target == "old" else "新版本"))
        )
        self.status_var.set("获取版本列表中...")

        thread = threading.Thread(target=self._do_fetch_versions, args=(project_path, vcs_type), daemon=True)
        thread.start()

    def _do_fetch_versions(self, project_path, vcs_type):
        try:
            info(f"获取版本列表: path={project_path}, vcs={vcs_type}")
            if vcs_type == "git":
                vcs = GitVCS(project_path)
                versions = vcs.get_versions()
            elif vcs_type == "git_multi":
                versions = GitMultiVersionVCS.get_recent_versions(project_path)
            elif vcs_type == "svn_multi":
                versions = SVNMultiVersionVCS.get_recent_versions(
                    project_path, svn_path=self.svn_path_var.get().strip()
                )
            else:
                vcs = SVNVCS(project_path, svn_path=self.svn_path_var.get().strip())
                versions = vcs.get_versions()
            info(f"获取到 {len(versions)} 个版本")

            def update_ui():
                if (self.vcs_var.get() != vcs_type or
                        self._normalize_project_path(self.dir_entry.get().strip()) !=
                        self._normalize_project_path(project_path)):
                    return
                self.version_listbox.delete(0, tk.END)
                if versions:
                    for v in versions:
                        self.version_listbox.insert(tk.END, v)
                    real_count = sum(1 for v in versions if not v.startswith("──"))
                    self.status_var.set(f"共 {real_count} 个版本，单击选中再点「填入」或直接双击")
                else:
                    self.version_listbox.insert(tk.END, "(未找到版本，请手动输入)")
                    self.status_var.set("未找到版本，请手动输入commit/revision号")
            self.root.after(0, update_ui)
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda msg=msg: messagebox.showerror("错误", f"获取版本列表失败:\n{msg}"))
            self.root.after(0, lambda: self.status_var.set("出错"))

    def _on_version_click(self, event):
        """单击选中列表项；双击直接填入"""
        # 使用 nearest(event.y) 准确获取点击位置对应的项
        idx = self.version_listbox.nearest(event.y)
        if idx < 0:
            return
        item = self.version_listbox.get(idx)
        if item.startswith("──") or item.startswith("(") or item.startswith("正在"):
            return
        if self.vcs_var.get() in ("git_multi", "svn_multi"):
            now = event.time
            last = getattr(self, '_last_click_time', 0)
            last_idx = getattr(self, '_last_click_idx', -1)
            self._last_click_time = now
            self._last_click_idx = idx
            if now - last < 400 and idx == last_idx:
                self._fill_selected_version()
            return
        # 选中该项
        self.version_listbox.selection_clear(0, tk.END)
        self.version_listbox.selection_set(idx)

        # 检测是否为双击（通过事件类型判断）
        if event.type == tk.EventType.ButtonRelease and hasattr(event, 'num'):
            # ButtonRelease-1: 检查是否在短时间内有两次点击（模拟双击）
            now = event.time
            last = getattr(self, '_last_click_time', 0)
            last_idx = getattr(self, '_last_click_idx', -1)
            self._last_click_time = now
            self._last_click_idx = idx
            if now - last < 400 and idx == last_idx:
                self._fill_selected_version()

    def _fill_selected_version(self):
        """将列表框中当前选中的版本填入对应的输入框"""
        sel = self.version_listbox.curselection()
        if not sel:
            return
        if self.vcs_var.get() in ("git_multi", "svn_multi"):
            versions = []
            for idx in sel:
                item = self.version_listbox.get(idx)
                if item.startswith("──") or item.startswith("(") or item.startswith("正在"):
                    continue
                versions.append(item.split(" ")[0] if " " in item else item)
            if versions:
                self.old_version_var.set("\n".join(versions))
            return
        item = self.version_listbox.get(sel[0])
        if item.startswith("──") or item.startswith("(") or item.startswith("正在"):
            return
        version = item.split(" ")[0] if " " in item else item
        if self._version_target == "new":
            self.new_version_var.set(version)
        else:
            self.old_version_var.set(version)

    # ========== 多项目任务 ==========

    def _task_source_text(self, task: dict) -> str:
        if task["vcs_type"] == "archive":
            return task.get("new_version", "")
        if task["vcs_type"] == "folder":
            return task.get("new_version", "")
        return task.get("project_path", "")

    def _task_versions_text(self, task: dict) -> str:
        old_version = (task.get("old_version") or "").replace("\n", ", ")
        new_version = (task.get("new_version") or "").replace("\n", ", ")
        if task["vcs_type"] in ("git_multi", "svn_multi"):
            return old_version
        return f"{old_version} → {new_version}"

    def _render_multi_tasks(self):
        if not hasattr(self, "multi_task_tree"):
            return
        self.multi_task_tree.delete(*self.multi_task_tree.get_children())
        for idx, task in enumerate(self._multi_tasks):
            self.multi_task_tree.insert(
                "", tk.END, iid=str(idx),
                values=(
                    task.get("project_name", ""),
                    self._vcs_label(task.get("vcs_type", "")),
                    self._task_source_text(task),
                    self._task_versions_text(task),
                )
            )

    def _selected_multi_task_index(self):
        sel = self.multi_task_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (TypeError, ValueError):
            return None

    def _task_from_current_form(self) -> dict:
        vcs_type = self.vcs_var.get()
        project_path = self.dir_entry.get().strip()
        old_version = self.old_version_var.get().strip()
        new_version = self.new_version_var.get().strip()
        is_folder = vcs_type == "folder"
        is_archive = vcs_type == "archive"
        is_multi = vcs_type in ("git_multi", "svn_multi")

        if is_archive:
            if not old_version or not os.path.isfile(old_version):
                raise ValueError("旧版本压缩包不存在，请选择有效的压缩包文件")
            if not new_version or not os.path.isfile(new_version):
                raise ValueError("新版本压缩包不存在，请选择有效的压缩包文件")
        elif is_folder:
            if not old_version or not os.path.isdir(old_version):
                raise ValueError("旧版本文件夹不存在，请选择有效的文件夹")
            if not new_version or not os.path.isdir(new_version):
                raise ValueError("新版本文件夹不存在，请选择有效的文件夹")
        else:
            if not project_path:
                raise ValueError("请选择项目目录")
            if not os.path.isdir(project_path):
                raise ValueError("项目目录不存在")
            if is_multi:
                selected_versions = parse_multi_versions(old_version)
                if not selected_versions:
                    raise ValueError("请选择或输入至少一个版本")
                old_version = ", ".join(selected_versions)
                new_version = "基线 + 选中版本"
            elif not old_version or not new_version:
                raise ValueError("请输入旧版本和新版本")

        project_name = self._sanitize_project_name(
            self.project_name_var.get().strip() or self._default_project_name()
        )
        if self.project_name_var.get().strip() != project_name:
            self.project_name_var.set(project_name)

        exclude_key = self._project_key_for_values(vcs_type, project_path, old_version, new_version)
        exclude_rules = self._current_exclude_rules()
        if exclude_key:
            self._project_exclude_rules[exclude_key] = exclude_rules

        return {
            "project_name": project_name,
            "vcs_type": vcs_type,
            "project_path": project_path,
            "old_version": old_version,
            "new_version": new_version,
            "svn_path": self.svn_path_var.get().strip(),
            "exclude_key": exclude_key,
            "exclude_rules": exclude_rules,
        }

    def _ensure_unique_task_name(self, project_name: str, editing_index=None) -> bool:
        for idx, task in enumerate(self._multi_tasks):
            if editing_index is not None and idx == editing_index:
                continue
            if task.get("project_name") == project_name:
                messagebox.showwarning("提示", f"多项目任务中已存在项目名: {project_name}\n请换一个项目名，避免报告和导出目录混淆。")
                return False
        return True

    def _add_or_update_multi_task(self):
        try:
            task = self._task_from_current_form()
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return

        if not self._ensure_unique_task_name(task["project_name"], self._editing_task_index):
            return

        if self._editing_task_index is None:
            self._multi_tasks.append(task)
            self.status_var.set(f"已添加多项目任务: {task['project_name']}")
        else:
            self._multi_tasks[self._editing_task_index] = task
            self.status_var.set(f"已更新多项目任务: {task['project_name']}")
            self._editing_task_index = None
            self.add_task_btn.config(text="添加到多项目任务")
            self.cancel_edit_btn.config(state=tk.DISABLED)

        self._render_multi_tasks()
        self._save_current_config()

    def _edit_multi_task(self):
        idx = self._selected_multi_task_index()
        if idx is None or idx < 0 or idx >= len(self._multi_tasks):
            messagebox.showinfo("提示", "请先选择要编辑的多项目任务")
            return

        self._save_current_exclude_rules_for_current_key()
        task = self._multi_tasks[idx]
        self._editing_task_index = idx
        self.add_task_btn.config(text="更新多项目任务")
        self.cancel_edit_btn.config(state=tk.NORMAL)

        self.vcs_var.set(task.get("vcs_type", "git"))
        self.dir_entry.delete(0, tk.END)
        self.dir_entry.insert(0, task.get("project_path", ""))
        self.old_version_var.set(task.get("old_version", ""))
        self.new_version_var.set(task.get("new_version", ""))
        self.svn_path_var.set(task.get("svn_path", ""))
        self.project_name_var.set(task.get("project_name", ""))
        self._project_name_manual = True
        self._replace_exclude_text(task.get("exclude_rules", self._default_exclude_rules))
        self._last_exclude_key = task.get("exclude_key", "")
        self._last_project_path = self._normalize_project_path(self.dir_entry.get().strip())
        self._update_output_paths()
        self.status_var.set(f"正在编辑多项目任务: {task.get('project_name', '')}")

    def _cancel_edit_task(self):
        self._editing_task_index = None
        self.add_task_btn.config(text="添加到多项目任务")
        self.cancel_edit_btn.config(state=tk.DISABLED)
        self.status_var.set("已取消编辑多项目任务")

    def _delete_multi_task(self):
        idx = self._selected_multi_task_index()
        if idx is None or idx < 0 or idx >= len(self._multi_tasks):
            messagebox.showinfo("提示", "请先选择要删除的多项目任务")
            return
        name = self._multi_tasks[idx].get("project_name", "")
        del self._multi_tasks[idx]
        self._cancel_edit_task()
        self._render_multi_tasks()
        self._save_current_config()
        self.status_var.set(f"已删除多项目任务: {name}")

    def _clear_multi_tasks(self):
        if not self._multi_tasks:
            return
        if not messagebox.askyesno("确认清空", "确定要清空所有多项目任务吗？"):
            return
        self._multi_tasks.clear()
        self._cancel_edit_task()
        self._render_multi_tasks()
        self._save_current_config()
        self.status_var.set("已清空多项目任务")

    # ========== 生成报告 ==========

    def _check_overwrite(self, project_name=""):
        """检查本项目导出子目录是否已有内容"""
        msgs = []

        report_path = self.report_path_var.get().strip()
        if report_path and os.path.exists(report_path):
            msgs.append(f"• 报告文件已存在:\n  {report_path}")

        old_export = self.old_export_var.get().strip()
        if old_export and project_name:
            old_target = os.path.join(old_export, project_name)
            if os.path.isdir(old_target) and os.listdir(old_target):
                msgs.append(f"• 旧版本导出目录已有本项目内容:\n  {old_target}")

        new_export = self.new_export_var.get().strip()
        if new_export and project_name:
            new_target = os.path.join(new_export, project_name)
            if os.path.isdir(new_target) and os.listdir(new_target):
                msgs.append(f"• 新版本导出目录已有本项目内容:\n  {new_target}")

        if msgs:
            return messagebox.askyesno(
                "确认清空并重新导出",
                "以下目标已有本项目内容，将被清空后重新导出：\n\n" + "\n\n".join(msgs)
            )
        return True

    def _check_multi_overwrite(self):
        old_export = self.old_export_var.get().strip()
        new_export = self.new_export_var.get().strip()
        msgs = []
        for task in self._multi_tasks:
            project_name = task.get("project_name", "")
            for base_dir, label in ((old_export, "旧版本"), (new_export, "新版本")):
                target = os.path.join(base_dir, project_name)
                if os.path.isdir(target) and os.listdir(target):
                    msgs.append(f"• {label}导出目录已有项目内容:\n  {target}")
        if msgs:
            return messagebox.askyesno(
                "确认清空并重新导出",
                "以下目标已有内容，将在对应项目导出时清空：\n\n" + "\n\n".join(msgs)
            )
        return True

    def _set_generating(self, generating: bool):
        state = tk.DISABLED if generating else tk.NORMAL
        self.generate_btn.config(state=state)
        if hasattr(self, "generate_multi_btn"):
            self.generate_multi_btn.config(state=state)
        if hasattr(self, "add_task_btn"):
            self.add_task_btn.config(state=state)

    def _multi_report_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return os.path.join(
            self.output_dir_var.get().strip(),
            f"multi_compare_report_{stamp}.html"
        )

    def _create_vcs_for_task(self, task: dict):
        vcs_type = task["vcs_type"]
        if vcs_type == "archive":
            return ArchiveVCS(task["old_version"], task["new_version"]), True
        if vcs_type == "folder":
            return FolderVCS(task["old_version"], task["new_version"]), False
        if vcs_type == "git_multi":
            return GitMultiVersionVCS(task["project_path"], parse_multi_versions(task["old_version"])), True
        if vcs_type == "svn_multi":
            return SVNMultiVersionVCS(
                task["project_path"],
                parse_multi_versions(task["old_version"]),
                svn_path=task.get("svn_path", "")
            ), True
        if vcs_type == "git":
            return GitVCS(task["project_path"]), False
        return SVNVCS(task["project_path"], svn_path=task.get("svn_path", "")), False

    def _prepare_task_result(self, task: dict, show_full: bool):
        vcs = None
        cleanup_needed = False
        try:
            vcs, cleanup_needed = self._create_vcs_for_task(task)
            exclude_text = task.get("exclude_rules", "").strip()
            if exclude_text:
                vcs.set_exclude_patterns(exclude_text.split("\n"))

            if task["vcs_type"] not in ("folder", "archive", "git_multi", "svn_multi"):
                if not vcs.check_version_exists(task["old_version"]):
                    raise RuntimeError(f"{task['project_name']} 旧版本不存在: {task['old_version']}")
                if not vcs.check_version_exists(task["new_version"]):
                    raise RuntimeError(f"{task['project_name']} 新版本不存在: {task['new_version']}")

            engine = DiffEngine(vcs, show_full_context=show_full)
            diff_result = engine.generate_diff(task["old_version"], task["new_version"])
            diff_result.project_name = task["project_name"]
            diff_result.vcs_type = self._vcs_label(task["vcs_type"])
            if task["vcs_type"] == "archive":
                diff_result.project_path = os.path.basename(task["new_version"])
            elif task["vcs_type"] == "folder":
                diff_result.project_path = task["new_version"]
            else:
                diff_result.project_path = task["project_path"]
            return {
                "task": task,
                "vcs": vcs,
                "cleanup_needed": cleanup_needed,
                "project_name": task["project_name"],
                "vcs_type": self._vcs_label(task["vcs_type"]),
                "diff_result": diff_result,
            }
        except Exception:
            if vcs and cleanup_needed:
                vcs.cleanup()
            raise

    def _generate(self):
        project_path = self.dir_entry.get().strip()
        old_version = self.old_version_var.get().strip()
        new_version = self.new_version_var.get().strip()
        vcs_type = self.vcs_var.get()
        is_folder = vcs_type == "folder"
        is_archive = vcs_type == "archive"
        is_multi = vcs_type in ("git_multi", "svn_multi")

        if is_archive:
            if not old_version or not os.path.isfile(old_version):
                messagebox.showwarning("提示", "旧版本压缩包不存在，请选择有效的压缩包文件")
                return
            if not new_version or not os.path.isfile(new_version):
                messagebox.showwarning("提示", "新版本压缩包不存在，请选择有效的压缩包文件")
                return
        elif is_folder:
            if not old_version or not os.path.isdir(old_version):
                messagebox.showwarning("提示", "旧版本文件夹不存在，请选择有效的文件夹")
                return
            if not new_version or not os.path.isdir(new_version):
                messagebox.showwarning("提示", "新版本文件夹不存在，请选择有效的文件夹")
                return
        elif is_multi:
            if not project_path:
                messagebox.showwarning("提示", "请选择项目目录")
                return
            if not os.path.isdir(project_path):
                messagebox.showwarning("提示", "项目目录不存在")
                return
            selected_versions = parse_multi_versions(old_version)
            if not selected_versions:
                messagebox.showwarning("提示", "请选择或输入至少一个版本")
                return
            old_version = ", ".join(selected_versions)
            new_version = "基线 + 选中版本"
        else:
            if not project_path:
                messagebox.showwarning("提示", "请选择项目目录")
                return
            if not os.path.isdir(project_path):
                messagebox.showwarning("提示", "项目目录不存在")
                return
            if not old_version or not new_version:
                messagebox.showwarning("提示", "请输入旧版本和新版本")
                return

        report_path = self.report_path_var.get().strip()
        old_export = self.old_export_var.get().strip()
        new_export = self.new_export_var.get().strip()

        if not report_path:
            messagebox.showwarning("提示", "请先选择输出目录")
            return

        project_name = self._sanitize_project_name(
            self.project_name_var.get().strip() or self._default_project_name()
        )
        if self.project_name_var.get().strip() != project_name:
            self.project_name_var.set(project_name)
        if not self._check_overwrite(project_name):
            return

        self._set_generating(True)
        self.progress.start()
        self.status_var.set("正在生成比对报告...")

        thread = threading.Thread(target=self._do_generate, args=(
            project_path, vcs_type, old_version, new_version, project_name
        ), daemon=True)
        thread.start()

    def _generate_multi(self):
        if not self._multi_tasks:
            messagebox.showwarning("提示", "请先添加至少一个多项目任务")
            return
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请先选择输出目录")
            return

        names = set()
        for task in self._multi_tasks:
            name = task.get("project_name", "")
            if name in names:
                messagebox.showwarning("提示", f"多项目任务存在重复项目名: {name}")
                return
            names.add(name)

        self._save_current_exclude_rules_for_current_key()
        self._save_current_config()

        self.old_export_var.set(os.path.join(output_dir, "oldVersion"))
        self.new_export_var.set(os.path.join(output_dir, "newVersion"))
        if not self._check_multi_overwrite():
            return

        report_path = self._multi_report_path()
        self._set_generating(True)
        self.progress.start()
        self.status_var.set("正在生成多项目总报告...")

        tasks = [dict(task) for task in self._multi_tasks]
        thread = threading.Thread(target=self._do_generate_multi, args=(tasks, report_path), daemon=True)
        thread.start()

    def _do_generate(self, project_path, vcs_type, old_version, new_version, project_name):
        cleanup_vcs = None  # 持有引用以便 finally 清理临时目录
        try:
            info(f"=== 开始生成比对报告 ===")
            info(f"project_path={project_path}, vcs_type={vcs_type}, old={old_version}, new={new_version}")

            if vcs_type == "archive":
                vcs = ArchiveVCS(old_version, new_version)
                cleanup_vcs = vcs
            elif vcs_type == "folder":
                vcs = FolderVCS(old_version, new_version)
            elif vcs_type == "git_multi":
                vcs = GitMultiVersionVCS(project_path, parse_multi_versions(old_version))
                cleanup_vcs = vcs
            elif vcs_type == "svn_multi":
                vcs = SVNMultiVersionVCS(project_path, parse_multi_versions(old_version),
                                         svn_path=self.svn_path_var.get().strip())
                cleanup_vcs = vcs
            elif vcs_type == "git":
                vcs = GitVCS(project_path)
            else:
                vcs = SVNVCS(project_path, svn_path=self.svn_path_var.get().strip())

            exclude_text = self.exclude_text.get("1.0", tk.END).strip()
            if exclude_text:
                vcs.set_exclude_patterns(exclude_text.split("\n"))

            if vcs_type not in ("folder", "archive", "git_multi", "svn_multi"):
                if not vcs.check_version_exists(old_version):
                    warn(f"旧版本不存在: {old_version}")
                    self._show_error(f"旧版本不存在: {old_version}")
                    return
                if not vcs.check_version_exists(new_version):
                    warn(f"新版本不存在: {new_version}")
                    self._show_error(f"新版本不存在: {new_version}")
                    return

            info("获取变更文件列表...")
            show_full = self.show_full_context_var.get() == "yes"
            engine = DiffEngine(vcs, show_full_context=show_full)
            diff_result = engine.generate_diff(old_version, new_version)
            diff_result.project_name = project_name
            if vcs_type == "archive":
                diff_result.project_path = os.path.basename(new_version)
            elif vcs_type in ("git_multi", "svn_multi"):
                diff_result.project_path = project_path
            info(f"变更文件数: {len(diff_result.files)}")

            report_path = self.report_path_var.get().strip()
            info(f"生成报告: {report_path}")
            template_dir = os.path.join(BASE_DIR, "templates")
            report_gen = ReportGenerator(template_dir)
            show_project_root = self.show_project_root_var.get() == "yes"
            report_gen.generate(diff_result, report_path, show_project_root=show_project_root)

            old_export = self.old_export_var.get().strip()
            new_export = self.new_export_var.get().strip()
            info(f"导出文件: old={old_export}, new={new_export}")
            exporter = FileExporter(diff_result, vcs)
            project_name = diff_result.project_name
            exporter.export(old_export, new_export, project_name=project_name)

            # 保存配置
            self._save_current_config()

            summary = diff_result.summary
            info(f"=== 完成: {summary} ===")
            self.root.after(0, lambda: self._on_complete(report_path, summary))

        except Exception as e:
            msg = str(e)
            error(f"生成失败: {msg}")
            import traceback; error(traceback.format_exc())
            self.root.after(0, lambda msg=msg: self._show_error(msg))
        finally:
            if cleanup_vcs:
                cleanup_vcs.cleanup()

    def _do_generate_multi(self, tasks, report_path):
        project_results = []
        try:
            info("=== 开始生成多项目总报告 ===")
            show_full = self.show_full_context_var.get() == "yes"
            for idx, task in enumerate(tasks, start=1):
                info(f"多项目任务 {idx}/{len(tasks)}: {task.get('project_name')} {task.get('vcs_type')}")
                self.root.after(0, lambda idx=idx, total=len(tasks), name=task.get("project_name", ""):
                                self.status_var.set(f"正在处理项目 {idx}/{total}: {name}"))
                project_results.append(self._prepare_task_result(task, show_full))

            old_export = self.old_export_var.get().strip()
            new_export = self.new_export_var.get().strip()
            for item in project_results:
                task = item["task"]
                exporter = FileExporter(item["diff_result"], item["vcs"])
                exporter.export(old_export, new_export, project_name=task["project_name"])

            template_dir = os.path.join(BASE_DIR, "templates")
            report_gen = ReportGenerator(template_dir)
            report_gen.generate_multi(project_results, report_path)

            summary = ReportGenerator._multi_summary(project_results)
            info(f"=== 多项目完成: {summary} ===")
            self.root.after(0, lambda: self._on_multi_complete(report_path, summary))
        except Exception as e:
            msg = str(e)
            error(f"多项目生成失败: {msg}")
            import traceback; error(traceback.format_exc())
            self.root.after(0, lambda msg=msg: self._show_error(msg))
        finally:
            for item in project_results:
                if item.get("cleanup_needed"):
                    try:
                        item["vcs"].cleanup()
                    except Exception:
                        pass

    def _on_complete(self, report_path, summary):
        self.progress.stop()
        self._set_generating(False)
        self.status_var.set(
            f"完成! 共 {summary['total_files']} 个文件变更 "
            f"(+{summary['total_added_lines']}/-{summary['total_deleted_lines']})"
        )
        if messagebox.askyesno("完成", f"比对报告已生成!\n\n"
                                       f"变更文件: {summary['total_files']} 个\n"
                                       f"新增行数: +{summary['total_added_lines']}\n"
                                       f"删除行数: -{summary['total_deleted_lines']}\n\n"
                                       f"是否打开报告?"):
            webbrowser.open(f"file:///{report_path}")

    def _on_multi_complete(self, report_path, summary):
        self.progress.stop()
        self._set_generating(False)
        self.status_var.set(
            f"多项目完成! {summary['project_count']} 个项目，"
            f"{summary['total_files']} 个文件变更 "
            f"(+{summary['total_added_lines']}/-{summary['total_deleted_lines']})"
        )
        if messagebox.askyesno("完成", f"多项目总报告已生成!\n\n"
                                       f"项目: {summary['project_count']} 个\n"
                                       f"变更文件: {summary['total_files']} 个\n"
                                       f"新增行数: +{summary['total_added_lines']}\n"
                                       f"删除行数: -{summary['total_deleted_lines']}\n\n"
                                       f"是否打开报告?"):
            webbrowser.open(f"file:///{report_path}")

    def _show_error(self, msg):
        self.progress.stop()
        self._set_generating(False)
        self.status_var.set("出错")
        messagebox.showerror("错误", msg)

    def _save_current_config(self):
        """保存当前界面配置到文件"""
        self._save_current_exclude_rules_for_current_key()
        data = {
            "project_path": self.dir_entry.get().strip(),
            "vcs_type": self.vcs_var.get(),
            "svn_path": self.svn_path_var.get().strip(),
            "project_exclude_rules": self._project_exclude_rules,
            "multi_tasks": self._multi_tasks,
            "output_dir": self.output_dir_var.get().strip(),
        }
        _save_config(data)

    def _on_close(self):
        self._save_current_config()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = CompareToolApp()
    app.run()


if __name__ == "__main__":
    main()
