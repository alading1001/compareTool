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
RECENT_PROJECT_LIMIT = 10

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
        self.root.minsize(600, 560)
        # 窗口居中显示
        w = 760
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        h = max(560, min(920, hs - 90))
        x = max(0, (ws - w) // 2)
        y = 0
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        icon_path = os.path.join(BASE_DIR, "assets", "icons", "app.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            self.root.iconbitmap(icon_path)

        self._default_output = os.path.join(os.path.expanduser("~"), "Desktop")
        self._update_after_id = None
        self._config = _load_config()
        self._default_exclude_rules = self._load_default_exclude_rules()
        self._project_exclude_rules = dict(self._config.get("project_exclude_rules", {}))
        self._project_display_options = dict(self._config.get("project_display_options", {}))
        self._recent_projects = self._normalize_recent_projects(self._config.get("recent_projects", []))
        self._recent_project_value_map = {}
        self._multi_tasks = list(self._config.get("multi_tasks", []))
        self._editing_task_index = None
        self._generating = False
        self._last_exclude_key = ""
        self._project_name_manual = False
        self._version_items = []
        self._selected_multi_versions = set()
        self._updating_version_list = False
        self._version_list_visible = True
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ========== UI 构建 ==========

    def _load_default_exclude_rules(self) -> str:
        """读取内置默认排除规则模板。项目专属规则由 project_exclude_rules 负责。"""
        return DEFAULT_EXCLUDE_RULES

    def _build_ui(self):
        bottom_frame = ttk.Frame(self.root, padding=(14, 6, 14, 8))
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.progress = ttk.Progressbar(bottom_frame, mode="indeterminate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))

        self.generate_btn = ttk.Button(bottom_frame, text="生成比对报告", command=self._generate)
        self.generate_btn.pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom_frame, textvariable=self.status_var, font=("", 9)).pack(side=tk.RIGHT, padx=(0, 12))

        container = ttk.Frame(self.root)
        container.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        main = ttk.Frame(canvas, padding=(14, 4, 14, 10))
        window_id = canvas.create_window((0, 0), window=main, anchor="nw")

        def _sync_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_frame_width(event):
            canvas.itemconfigure(window_id, width=event.width)

        main.bind("<Configure>", _sync_scroll_region)
        canvas.bind("<Configure>", _sync_frame_width)

        def _on_main_mousewheel(event):
            if self._should_skip_main_mousewheel(event.widget):
                return None
            units = int(-1 * (event.delta / 120))
            if units == 0:
                units = -1 if event.delta > 0 else 1
            first, last = canvas.yview()
            if (units < 0 and first <= 0) or (units > 0 and last >= 1):
                return "break"
            canvas.yview_scroll(units, "units")
            return "break"

        canvas.bind_all("<MouseWheel>", _on_main_mousewheel)

        # ── VCS 类型 ──
        ttk.Label(main, text="版本控制类型:", font=("", 10)).grid(row=0, column=0, sticky=tk.W, pady=(0, 3))
        vcs_frame = ttk.Frame(main)
        vcs_frame.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        vcs_buttons_frame = ttk.Frame(vcs_frame)
        vcs_buttons_frame.pack(anchor=tk.W)
        self.vcs_var = tk.StringVar(value=self._config.get("vcs_type", "git"))
        self.vcs_var.trace_add("write", lambda *_: self._on_vcs_changed())
        ttk.Radiobutton(vcs_buttons_frame, text="Git", variable=self.vcs_var, value="git").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_buttons_frame, text="SVN", variable=self.vcs_var, value="svn").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_buttons_frame, text="文件夹", variable=self.vcs_var, value="folder").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_buttons_frame, text="压缩包", variable=self.vcs_var, value="archive").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_buttons_frame, text="Git需求包", variable=self.vcs_var, value="git_multi").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(vcs_buttons_frame, text="SVN需求包", variable=self.vcs_var, value="svn_multi").pack(side=tk.LEFT)
        self.vcs_help_var = tk.StringVar()
        self.vcs_help_label = ttk.Label(
            vcs_frame,
            textvariable=self.vcs_help_var,
            foreground="#555555",
            justify=tk.LEFT,
            wraplength=720
        )
        self.vcs_help_label.pack(anchor=tk.W, fill=tk.X, pady=(3, 0))
        vcs_frame.bind(
            "<Configure>",
            lambda e: self.vcs_help_label.configure(wraplength=max(360, e.width - 4))
        )

        # ── 项目目录 ──
        self.project_label = ttk.Label(main, text="项目目录:", font=("", 10))
        self.project_label.grid(row=2, column=0, sticky=tk.W, pady=(0, 2))
        self.project_dir_frame = ttk.Frame(main)
        self.project_dir_frame.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.dir_entry = ttk.Entry(self.project_dir_frame)
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.dir_entry.bind("<KeyRelease>", lambda e: self._on_project_path_changed())
        self.dir_entry.bind("<FocusOut>", lambda e: self._on_project_path_changed())
        ttk.Button(self.project_dir_frame, text="浏览...", command=self._browse_project).pack(side=tk.LEFT, padx=(6, 0))
        # 恢复上次项目路径
        last_project = self._config.get("project_path", "")
        if last_project:
            self.dir_entry.insert(0, last_project)

        # ── 报告项目名 ──
        ttk.Label(main, text="项目名 (报告树/变更清单/导出目录使用):", font=("", 10)).grid(row=4, column=0, sticky=tk.W, pady=(0, 3))
        project_name_frame = ttk.Frame(main)
        project_name_frame.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.project_name_var = tk.StringVar()
        self.project_name_entry = ttk.Combobox(project_name_frame, textvariable=self.project_name_var)
        self.project_name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.project_name_entry.bind("<KeyRelease>", lambda e: setattr(self, "_project_name_manual", True))
        self.project_name_entry.bind("<<ComboboxSelected>>", self._on_recent_project_selected)

        # ── 排除规则 ──
        ttk.Label(main, text="排除规则 (每行一个，支持 * 和 ** 通配符):", font=("", 10)).grid(row=6, column=0, sticky=tk.W, pady=(0, 3))
        exclude_frame = ttk.Frame(main)
        exclude_frame.grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
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
        old_frame.grid(row=9, column=0, columnspan=3, sticky=tk.EW, pady=(0, 5))
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
        new_frame.grid(row=11, column=0, columnspan=3, sticky=tk.EW, pady=(0, 5))
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
        self.version_listbox.bind("<<ListboxSelect>>", self._on_version_selection_changed)

        fill_btn_frame = ttk.Frame(main)
        fill_btn_frame.grid(row=13, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        fill_btn_frame.grid_remove()
        self.fill_btn_frame = fill_btn_frame
        self.fill_target_label = ttk.Label(fill_btn_frame, text="", font=("", 9))
        self.fill_target_label.pack(side=tk.LEFT)
        ttk.Label(fill_btn_frame, text="搜索:").pack(side=tk.LEFT, padx=(14, 4))
        self.version_search_var = tk.StringVar()
        self.version_search_var.trace_add("write", self._apply_version_filter)
        self.version_search_entry = ttk.Entry(fill_btn_frame, textvariable=self.version_search_var, width=28)
        self.version_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.clear_version_search_btn = ttk.Button(fill_btn_frame, text="清空", command=self._clear_version_filter)
        self.clear_version_search_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.toggle_version_list_btn = ttk.Button(fill_btn_frame, text="隐藏版本列表", command=self._toggle_version_list)
        self.toggle_version_list_btn.pack(side=tk.RIGHT)
        self.fill_selected_btn = ttk.Button(fill_btn_frame, text="← 填入选中版本", command=self._fill_selected_version)
        self.fill_selected_btn.pack(side=tk.RIGHT, padx=(0, 6))

        self._version_target = "old"

        # ── 输出路径 ──
        ttk.Label(main, text="输出路径设置:", font=("", 10, "bold")).grid(row=14, column=0, sticky=tk.W, pady=(6, 3))

        ttk.Label(main, text="输出目录:").grid(row=15, column=0, sticky=tk.W)
        output_dir_frame = ttk.Frame(main)
        output_dir_frame.grid(row=16, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        self.output_dir_var = tk.StringVar(value=self._config.get("output_dir", ""))
        self.output_dir_var.trace_add("write", lambda *_: self._update_output_paths())
        ttk.Entry(output_dir_frame, textvariable=self.output_dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(output_dir_frame, text="浏览...", command=lambda: self._browse_dir(self.output_dir_var)).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(main, text="输出批次名称 (可选):").grid(row=17, column=0, sticky=tk.W)
        batch_frame = ttk.Frame(main)
        batch_frame.grid(row=18, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        self.output_batch_var = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))
        self.output_batch_var.trace_add("write", lambda *_: self._update_output_paths())
        ttk.Entry(batch_frame, textvariable=self.output_batch_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.report_path_var = tk.StringVar()
        self.old_export_var = tk.StringVar()
        self.new_export_var = tk.StringVar()

        # ── 显示选项 ──
        ttk.Label(main, text="显示选项:", font=("", 10, "bold")).grid(row=19, column=0, sticky=tk.W, pady=(6, 3))
        show_root_frame = ttk.Frame(main)
        show_root_frame.grid(row=20, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        show_root_choice_frame = ttk.Frame(show_root_frame)
        show_root_choice_frame.pack(anchor=tk.W)
        ttk.Label(show_root_choice_frame, text="报告树及变更清单使用项目名:").pack(side=tk.LEFT)
        self.show_project_root_var = tk.StringVar(value="yes")
        ttk.Radiobutton(show_root_choice_frame, text="是", variable=self.show_project_root_var, value="yes").pack(side=tk.LEFT, padx=(6, 2))
        ttk.Radiobutton(show_root_choice_frame, text="否", variable=self.show_project_root_var, value="no").pack(side=tk.LEFT)
        self.show_project_root_help_label = ttk.Label(
            show_root_frame,
            text="是：路径以项目名开头；否：只显示项目内部路径。",
            foreground="#555555",
            justify=tk.LEFT,
            wraplength=720
        )
        self.show_project_root_help_label.pack(anchor=tk.W, fill=tk.X, pady=(2, 0))
        show_root_frame.bind(
            "<Configure>",
            lambda e: self.show_project_root_help_label.configure(wraplength=max(360, e.width - 4))
        )

        show_ctx_frame = ttk.Frame(main)
        show_ctx_frame.grid(row=21, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        ttk.Label(show_ctx_frame, text="差异展示方式:").pack(side=tk.LEFT)
        self.show_full_context_var = tk.StringVar(value="yes")
        ttk.Radiobutton(show_ctx_frame, text="全部内容", variable=self.show_full_context_var, value="yes").pack(side=tk.LEFT, padx=(6, 2))
        ttk.Radiobutton(show_ctx_frame, text="仅差异上下文", variable=self.show_full_context_var, value="no").pack(side=tk.LEFT)

        # ── 多项目批量任务 ──
        ttk.Label(main, text="多项目批量任务:", font=("", 10, "bold")).grid(row=24, column=0, sticky=tk.W, pady=(6, 3))
        multi_btn_frame = ttk.Frame(main)
        multi_btn_frame.grid(row=25, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        self.add_task_btn = ttk.Button(multi_btn_frame, text="添加到多项目任务", command=self._add_or_update_multi_task)
        self.add_task_btn.pack(side=tk.LEFT)
        self.cancel_edit_btn = ttk.Button(multi_btn_frame, text="取消编辑", command=self._cancel_edit_task, state=tk.DISABLED)
        self.cancel_edit_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.edit_task_btn = ttk.Button(multi_btn_frame, text="编辑任务", command=self._edit_multi_task)
        self.edit_task_btn.pack(side=tk.LEFT, padx=(12, 0))
        self.delete_task_btn = ttk.Button(multi_btn_frame, text="删除任务", command=self._delete_multi_task)
        self.delete_task_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.clear_tasks_btn = ttk.Button(multi_btn_frame, text="清空任务", command=self._clear_multi_tasks)
        self.clear_tasks_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.generate_multi_btn = ttk.Button(multi_btn_frame, text="生成多项目总报告", command=self._generate_multi)
        self.generate_multi_btn.pack(side=tk.RIGHT)

        multi_list_frame = ttk.Frame(main)
        multi_list_frame.grid(row=26, column=0, columnspan=3, sticky=tk.EW, pady=(0, 5))
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
        self.multi_task_tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_multi_task_buttons())

        main.columnconfigure(0, weight=1)

        # 初始化输出路径和 VCS UI
        self._render_multi_tasks()
        self._on_vcs_changed()
        self._last_project_path = self._normalize_project_path(self.dir_entry.get().strip())
        self._refresh_project_name_default(force=True)
        self._switch_exclude_rules_for_current_source(save_previous=False)
        self._remember_recent_project(refresh=True)

    # ========== 界面交互 ==========

    @staticmethod
    def _normalize_project_path(path: str) -> str:
        if not path:
            return ""
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))

    def _is_project_vcs_mode(self) -> bool:
        return self.vcs_var.get() in ("git", "svn", "git_multi", "svn_multi")

    @staticmethod
    def _vcs_family(vcs_type: str) -> str:
        if vcs_type in ("git", "git_multi"):
            return "git"
        if vcs_type in ("svn", "svn_multi"):
            return "svn"
        return ""

    def _normalize_recent_projects(self, projects) -> list:
        normalized = []
        seen = set()
        if not isinstance(projects, list):
            return normalized
        for item in projects:
            if not isinstance(item, dict):
                continue
            family = item.get("vcs_family") or self._vcs_family(item.get("vcs_type", ""))
            project_path = (item.get("project_path") or "").strip()
            project_name = self._sanitize_project_name(item.get("project_name") or "")
            if family not in ("git", "svn") or not project_path or not project_name:
                continue
            key = (family, self._normalize_project_path(project_path))
            if key in seen:
                continue
            seen.add(key)
            normalized.append({
                "vcs_family": family,
                "project_path": project_path,
                "project_name": project_name,
                "last_used": item.get("last_used") or "",
            })
        return self._trim_recent_projects(normalized)

    def _trim_recent_projects(self, projects: list) -> list:
        counts = {}
        trimmed = []
        for item in projects:
            family = item.get("vcs_family", "")
            count = counts.get(family, 0)
            if count >= RECENT_PROJECT_LIMIT:
                continue
            counts[family] = count + 1
            trimmed.append(item)
        return trimmed

    def _recent_project_display(self, item: dict) -> str:
        return f"{item.get('project_name', '')}    {self._display_path(item.get('project_path', ''))}"

    def _refresh_recent_project_values(self):
        if not hasattr(self, "project_name_entry"):
            return
        family = self._vcs_family(self.vcs_var.get())
        values = []
        self._recent_project_value_map = {}
        if family:
            for item in self._recent_projects:
                if item.get("vcs_family") != family:
                    continue
                display = self._recent_project_display(item)
                values.append(display)
                self._recent_project_value_map[display] = item
        self.project_name_entry.configure(values=values)

    def _remember_recent_project(
            self,
            vcs_type: str = None,
            project_path: str = None,
            project_name: str = None,
            refresh: bool = True):
        vcs_type = vcs_type or self.vcs_var.get()
        family = self._vcs_family(vcs_type)
        if not family:
            return
        project_path = (project_path if project_path is not None else self.dir_entry.get().strip()).strip()
        project_name = self._sanitize_project_name(
            project_name if project_name is not None else self._current_project_name_for_output()
        )
        if not project_path or not project_name or not os.path.isdir(project_path):
            return
        norm_path = self._normalize_project_path(project_path)
        entry = {
            "vcs_family": family,
            "project_path": project_path,
            "project_name": project_name,
            "last_used": datetime.now().isoformat(timespec="seconds"),
        }
        remaining = [
            item for item in self._recent_projects
            if not (
                item.get("vcs_family") == family and
                self._normalize_project_path(item.get("project_path", "")) == norm_path
            )
        ]
        self._recent_projects = self._trim_recent_projects([entry] + remaining)
        if refresh:
            self._refresh_recent_project_values()

    def _on_recent_project_selected(self, _event=None):
        item = self._recent_project_value_map.get(self.project_name_var.get())
        if not item:
            return
        old_path = self._normalize_project_path(self.dir_entry.get().strip())
        new_path = item.get("project_path", "")
        project_name = item.get("project_name", "")
        self.dir_entry.delete(0, tk.END)
        self.dir_entry.insert(0, new_path)
        if self._normalize_project_path(new_path) != old_path:
            self._on_project_path_changed()
        self.project_name_var.set(project_name)
        self._project_name_manual = True
        self._update_output_paths()

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
    def _vcs_help_text(vcs_type: str) -> str:
        return {
            "git": "比较同一 Git 仓库旧版本之后到新版本为止的变化（不含旧版本，含新版本）。",
            "svn": "比较同一 SVN 项目旧 revision 之后到新 revision 为止的变化（不含旧 revision，含新 revision）。",
            "folder": "选择旧文件夹和新文件夹，直接比较两个文件夹里的内容差异。",
            "archive": "选择旧压缩包和新压缩包，解压后比较压缩包里的内容差异。",
            "git_multi": "选择一个或多个 Git 提交，报告只体现这些选中提交带来的需求改动。",
            "svn_multi": "选择一个或多个 SVN revision，报告只体现这些选中 revision 带来的需求改动。",
        }.get(vcs_type, "")

    @staticmethod
    def _sanitize_project_name(name: str) -> str:
        raw = (name or "").strip()
        meaningful = "".join(ch for ch in raw if ch not in '<>:"/\\|?*').strip(" ._")
        if not meaningful:
            return ""
        cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in raw)
        return cleaned.strip(" .")

    @staticmethod
    def _sanitize_output_batch_name(name: str) -> str:
        cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in (name or "").strip())
        return cleaned.strip(" .")

    @staticmethod
    def _should_skip_main_mousewheel(widget) -> bool:
        scrollable_classes = (tk.Listbox, tk.Text, ttk.Treeview)
        while widget is not None:
            if isinstance(widget, scrollable_classes):
                return True
            widget = getattr(widget, "master", None)
        return False

    def _replace_exclude_text(self, text: str):
        self.exclude_text.delete("1.0", tk.END)
        self.exclude_text.insert("1.0", text or "")

    def _current_exclude_rules(self) -> str:
        return self.exclude_text.get("1.0", tk.END).strip()

    def _current_display_options(self) -> dict:
        return {
            "show_project_root": self.show_project_root_var.get(),
            "show_full_context": self.show_full_context_var.get(),
        }

    def _current_output_batch_name(self) -> str:
        if not hasattr(self, "output_batch_var"):
            return ""
        return self._sanitize_output_batch_name(self.output_batch_var.get())

    def _normalize_output_batch_field(self) -> str:
        batch_name = self._current_output_batch_name()
        if hasattr(self, "output_batch_var") and self.output_batch_var.get().strip() != batch_name:
            self.output_batch_var.set(batch_name)
        return batch_name

    @staticmethod
    def _option_bool(value, default=True) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            value = value.strip().lower()
            if value in ("yes", "true", "1", "y", "on"):
                return True
            if value in ("no", "false", "0", "n", "off"):
                return False
        return default

    @staticmethod
    def _option_value(value, default=True) -> str:
        return "yes" if CompareToolApp._option_bool(value, default=default) else "no"

    def _apply_display_options(self, options: dict):
        options = options or {}
        self.show_project_root_var.set(self._option_value(options.get("show_project_root"), default=True))
        self.show_full_context_var.set(self._option_value(options.get("show_full_context"), default=True))

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
        self._save_current_project_settings_for_current_key()

    def _save_current_project_settings_for_current_key(self):
        key = self._current_project_key()
        if key:
            self._project_exclude_rules[key] = self._current_exclude_rules()
            self._project_display_options[key] = self._current_display_options()

    def _switch_exclude_rules_for_current_source(self, save_previous: bool = True):
        key = self._current_project_key()
        old_key = getattr(self, "_last_exclude_key", "")
        if key == old_key:
            return
        if save_previous and old_key:
            self._project_exclude_rules[old_key] = self._current_exclude_rules()
            self._project_display_options[old_key] = self._current_display_options()
        rules = self._project_exclude_rules.get(key, self._default_exclude_rules) if key else self._default_exclude_rules
        options = self._project_display_options.get(key, {}) if key else {}
        self._replace_exclude_text(rules)
        self._apply_display_options(options)
        self._last_exclude_key = key

    def _default_project_name(self) -> str:
        vcs_type = self.vcs_var.get()
        project_path = self.dir_entry.get().strip()
        old_version = self.old_version_var.get().strip()
        new_version = self.new_version_var.get().strip()
        if vcs_type == "archive":
            source = new_version
            if not source or not os.path.isfile(source):
                return ""
            name = os.path.basename(source)
            for suffix in (".tar.gz", ".tar.bz2"):
                if name.lower().endswith(suffix):
                    return name[:-len(suffix)]
            return os.path.splitext(name)[0] if name else ""
        if vcs_type == "folder":
            source = new_version or old_version
            if not source or not os.path.isdir(source):
                return ""
            return os.path.basename(os.path.normpath(source))
        if not project_path or not os.path.isdir(project_path):
            return ""
        return os.path.basename(os.path.normpath(project_path))

    def _refresh_project_name_default(self, force: bool = False):
        if force or not self._project_name_manual or not self.project_name_var.get().strip():
            self.project_name_var.set(self._sanitize_project_name(self._default_project_name()))

    def _current_project_name_for_output(self) -> str:
        return self._sanitize_project_name(
            self.project_name_var.get().strip() or self._default_project_name()
        )

    def _require_project_name(self) -> str:
        project_name = self._current_project_name_for_output()
        if not project_name:
            raise ValueError("无法自动识别项目名，请检查项目路径/版本路径，或手动填写项目名")
        if self.project_name_var.get().strip() != project_name:
            self.project_name_var.set(project_name)
        return project_name

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
                self._reset_version_list_state()
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

    def _refresh_output_paths_now(self):
        if not hasattr(self, "output_dir_var"):
            return
        if self._update_after_id:
            try:
                self.root.after_cancel(self._update_after_id)
            except tk.TclError:
                pass
            self._update_after_id = None
        self._do_update_output_paths()
        if self._update_after_id:
            try:
                self.root.after_cancel(self._update_after_id)
            except tk.TclError:
                pass
            self._update_after_id = None

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

        batch_name = self._normalize_output_batch_field()

        effective_output_dir = self._effective_output_dir(output_dir, batch_name)
        project_name = self._current_project_name_for_output()

        self.report_path_var.set(self._join_display_path(effective_output_dir, f"{project_name}_diff.html") if project_name else "")
        self.old_export_var.set(self._join_display_path(effective_output_dir, "oldVersion"))
        self.new_export_var.set(self._join_display_path(effective_output_dir, "newVersion"))

    def _effective_output_dir(self, output_dir: str = "", batch_name: str = None) -> str:
        output_dir = output_dir or self.output_dir_var.get().strip()
        if not output_dir:
            return ""
        if batch_name is None:
            batch_name = self._current_output_batch_name()
        path = os.path.join(output_dir, batch_name) if batch_name else output_dir
        return self._display_path(path)

    @staticmethod
    def _display_path(path: str) -> str:
        return os.path.normpath(path).replace("\\", "/") if path else ""

    def _join_display_path(self, *parts: str) -> str:
        return self._display_path(os.path.join(*[p for p in parts if p]))

    def _on_vcs_changed(self):
        """VCS 类型切换时更新界面"""
        vcs_type = self.vcs_var.get()
        is_folder = vcs_type == "folder"
        is_archive = vcs_type == "archive"
        is_multi = vcs_type in ("git_multi", "svn_multi")
        self.vcs_help_var.set(self._vcs_help_text(vcs_type))
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
        self._reset_version_list_state()
        self._refresh_recent_project_values()

        if is_folder:
            self.project_label.grid_remove()
            self.project_dir_frame.grid_remove()
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
        self._refresh_recent_project_values()
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

    def _browse_save_file(self, var, desc, ext):
        path = filedialog.asksaveasfilename(title=f"保存{desc}", filetypes=[(desc, ext)], defaultextension=ext)
        if path:
            var.set(path)

    @staticmethod
    def _is_version_placeholder(item: str) -> bool:
        text = (item or "").strip()
        return (
            not text or
            text.startswith("——") or
            text.startswith("──") or
            text.startswith("(") or
            text.startswith("正在")
        )

    @staticmethod
    def _version_token(item: str) -> str:
        item = (item or "").strip()
        return item.split(" ")[0] if " " in item else item

    def _reset_version_list_state(self):
        self._version_items = []
        self._selected_multi_versions.clear()
        self._version_list_visible = True
        if hasattr(self, "version_listbox"):
            self.version_listbox.delete(0, tk.END)
        if hasattr(self, "version_search_var"):
            self.version_search_var.set("")
            self._refresh_version_list_visibility()

    def _clear_version_filter(self):
        self.version_search_var.set("")

    def _refresh_version_list_visibility(self):
        if not hasattr(self, "version_listbox"):
            return
        if self._version_list_visible:
            if self.fill_btn_frame.winfo_ismapped() and self.version_listbox.size() > 0:
                self.version_listbox.grid()
            self.toggle_version_list_btn.config(text="隐藏版本列表")
        else:
            self.version_listbox.grid_remove()
            self.toggle_version_list_btn.config(text="显示版本列表")

    def _set_version_list_visible(self, visible: bool):
        self._version_list_visible = visible
        self._refresh_version_list_visibility()

    def _toggle_version_list(self):
        if self._version_list_visible:
            self._sync_selected_multi_versions_from_listbox()
            self._set_version_list_visible(False)
            self.status_var.set("版本列表已隐藏")
        else:
            self._set_version_list_visible(True)
            if self._version_items:
                self._apply_version_filter()
            else:
                self.status_var.set("版本列表已显示")

    def _sync_selected_multi_versions_from_listbox(self):
        if self.vcs_var.get() not in ("git_multi", "svn_multi") or self._updating_version_list:
            return
        visible_tokens = []
        for idx in range(self.version_listbox.size()):
            item = self.version_listbox.get(idx)
            if self._is_version_placeholder(item):
                continue
            token = self._version_token(item)
            if token:
                visible_tokens.append(token)
        for token in visible_tokens:
            self._selected_multi_versions.discard(token)
        for idx in self.version_listbox.curselection():
            item = self.version_listbox.get(idx)
            if self._is_version_placeholder(item):
                continue
            token = self._version_token(item)
            if token:
                self._selected_multi_versions.add(token)

    def _on_version_selection_changed(self, _event=None):
        self._sync_selected_multi_versions_from_listbox()

    def _render_version_items(self, items):
        self._updating_version_list = True
        try:
            self.version_listbox.delete(0, tk.END)
            for item in items:
                self.version_listbox.insert(tk.END, item)
            if self.vcs_var.get() in ("git_multi", "svn_multi"):
                for idx in range(self.version_listbox.size()):
                    item = self.version_listbox.get(idx)
                    token = self._version_token(item)
                    if token in self._selected_multi_versions:
                        self.version_listbox.selection_set(idx)
        finally:
            self._updating_version_list = False
        self._refresh_version_list_visibility()

    def _apply_version_filter(self, *_):
        if not hasattr(self, "version_listbox") or not self._version_items:
            return
        self._sync_selected_multi_versions_from_listbox()
        keyword = self.version_search_var.get().strip().lower()
        if keyword:
            items = [
                item for item in self._version_items
                if not self._is_version_placeholder(item) and keyword in item.lower()
            ]
            self._render_version_items(items or ["(未找到匹配版本)"])
            self.status_var.set(f"匹配 {len(items)} 个版本")
        else:
            self._render_version_items(self._version_items)
            real_count = sum(1 for item in self._version_items if not self._is_version_placeholder(item))
            self.status_var.set(f"共 {real_count} 个版本")

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

        self._reset_version_list_state()
        self.version_listbox.delete(0, tk.END)
        self.version_listbox.insert(tk.END, "正在获取版本列表，请稍候...")
        self.fill_btn_frame.grid()
        self._set_version_list_visible(True)
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
                versions = SVNMultiVersionVCS.get_recent_versions(project_path)
            else:
                vcs = SVNVCS(project_path)
                versions = vcs.get_versions()
            info(f"获取到 {len(versions)} 个版本")

            def update_ui():
                if (self.vcs_var.get() != vcs_type or
                        self._normalize_project_path(self.dir_entry.get().strip()) !=
                        self._normalize_project_path(project_path)):
                    return
                self._version_items = list(versions)
                if versions:
                    self._apply_version_filter()
                    real_count = sum(1 for v in versions if not self._is_version_placeholder(v))
                    self.status_var.set(f"共 {real_count} 个版本，单击选中再点「填入」或直接双击")
                else:
                    self._render_version_items(["(未找到版本，请手动输入)"])
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
        if self._is_version_placeholder(item):
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
        is_multi = self.vcs_var.get() in ("git_multi", "svn_multi")
        if not sel and not (is_multi and self._selected_multi_versions):
            return
        if is_multi:
            self._sync_selected_multi_versions_from_listbox()
            versions = []
            if self._selected_multi_versions:
                for item in self._version_items:
                    if self._is_version_placeholder(item):
                        continue
                    token = self._version_token(item)
                    if token in self._selected_multi_versions:
                        versions.append(token)
            else:
                for idx in sel:
                    item = self.version_listbox.get(idx)
                    if self._is_version_placeholder(item):
                        continue
                    versions.append(self._version_token(item))
            if versions:
                self.old_version_var.set("\n".join(versions))
            return
        item = self.version_listbox.get(sel[0])
        if self._is_version_placeholder(item):
            return
        version = self._version_token(item)
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
        self._sync_multi_task_buttons()

    def _selected_multi_task_index(self):
        sel = self.multi_task_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (TypeError, ValueError):
            return None

    def _sync_multi_task_buttons(self):
        if not hasattr(self, "add_task_btn"):
            return

        generating = getattr(self, "_generating", False)
        editing = self._editing_task_index is not None
        selected = self._selected_multi_task_index()
        has_selection = selected is not None and 0 <= selected < len(self._multi_tasks)
        has_tasks = bool(self._multi_tasks)

        if generating:
            add_state = cancel_state = edit_state = delete_state = clear_state = multi_state = tk.DISABLED
        else:
            add_state = tk.NORMAL
            cancel_state = tk.NORMAL if editing else tk.DISABLED
            edit_state = tk.DISABLED if editing or not has_selection else tk.NORMAL
            delete_state = tk.DISABLED if editing or not has_selection else tk.NORMAL
            clear_state = tk.DISABLED if editing or not has_tasks else tk.NORMAL
            multi_state = tk.DISABLED if editing or not has_tasks else tk.NORMAL

        self.add_task_btn.config(
            text="更新多项目任务" if editing else "添加到多项目任务",
            state=add_state,
        )
        self.cancel_edit_btn.config(state=cancel_state)
        self.edit_task_btn.config(state=edit_state)
        self.delete_task_btn.config(state=delete_state)
        self.clear_tasks_btn.config(state=clear_state)
        self.generate_multi_btn.config(state=multi_state)

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

        project_name = self._require_project_name()

        exclude_key = self._project_key_for_values(vcs_type, project_path, old_version, new_version)
        exclude_rules = self._current_exclude_rules()
        if exclude_key:
            self._project_exclude_rules[exclude_key] = exclude_rules
            self._project_display_options[exclude_key] = self._current_display_options()

        return {
            "project_name": project_name,
            "vcs_type": vcs_type,
            "project_path": project_path,
            "old_version": old_version,
            "new_version": new_version,
            "exclude_key": exclude_key,
            "exclude_rules": exclude_rules,
            "show_project_root": self.show_project_root_var.get() == "yes",
            "show_full_context": self.show_full_context_var.get() == "yes",
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

        self._remember_recent_project(
            task["vcs_type"],
            task["project_path"],
            task["project_name"],
        )
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

        self.vcs_var.set(task.get("vcs_type", "git"))
        self.dir_entry.delete(0, tk.END)
        self.dir_entry.insert(0, task.get("project_path", ""))
        self.old_version_var.set(task.get("old_version", ""))
        self.new_version_var.set(task.get("new_version", ""))
        self.project_name_var.set(task.get("project_name", ""))
        self.show_project_root_var.set(self._option_value(task.get("show_project_root"), default=True))
        self.show_full_context_var.set(self._option_value(task.get("show_full_context"), default=True))
        self._project_name_manual = True
        self._replace_exclude_text(task.get("exclude_rules", self._default_exclude_rules))
        self._last_exclude_key = task.get("exclude_key", "")
        self._last_project_path = self._normalize_project_path(self.dir_entry.get().strip())
        self._update_output_paths()
        self._sync_multi_task_buttons()
        self.status_var.set(f"正在编辑多项目任务: {task.get('project_name', '')}")

    def _cancel_edit_task(self):
        self._editing_task_index = None
        self._sync_multi_task_buttons()
        self.status_var.set("已取消编辑多项目任务")

    def _delete_multi_task(self):
        idx = self._selected_multi_task_index()
        if idx is None or idx < 0 or idx >= len(self._multi_tasks):
            messagebox.showinfo("提示", "请先选择要删除的多项目任务")
            return
        name = self._multi_tasks[idx].get("project_name", "")
        del self._multi_tasks[idx]
        self._editing_task_index = None
        self._render_multi_tasks()
        self._save_current_config()
        self.status_var.set(f"已删除多项目任务: {name}")

    def _clear_multi_tasks(self):
        if not self._multi_tasks:
            return
        if not messagebox.askyesno("确认清空", "确定要清空所有多项目任务吗？"):
            return
        self._multi_tasks.clear()
        self._editing_task_index = None
        self._render_multi_tasks()
        self._save_current_config()
        self.status_var.set("已清空多项目任务")

    # ========== 生成报告 ==========

    def _confirm_output_batch(self) -> bool:
        self._refresh_output_paths_now()
        output_dir = self.output_dir_var.get().strip()
        batch_name = self._normalize_output_batch_field()
        effective_output_dir = self._effective_output_dir(output_dir, batch_name)
        if batch_name:
            msg = (
                f"本次输出批次名称：{batch_name}\n\n"
                f"实际输出目录：\n{effective_output_dir}\n\n"
                "若实际输出目录下已有同名报告或同项目导出内容，将自动覆盖/清空并重新生成。\n\n"
                "请确认批次是否正确，是否继续生成？"
            )
        else:
            msg = (
                "本次未设置输出批次名称，将直接输出到：\n"
                f"{effective_output_dir}\n\n"
                "若实际输出目录下已有同名报告或同项目导出内容，将自动覆盖/清空并重新生成。\n\n"
                "是否继续生成？"
            )
        return messagebox.askyesno("确认输出批次", msg)

    def _set_generating(self, generating: bool):
        self._generating = generating
        state = tk.DISABLED if generating else tk.NORMAL
        self.generate_btn.config(state=state)
        self._sync_multi_task_buttons()

    def _multi_report_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return self._join_display_path(
            self._effective_output_dir(),
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
                parse_multi_versions(task["old_version"])
            ), True
        if vcs_type == "git":
            return GitVCS(task["project_path"]), False
        return SVNVCS(task["project_path"]), False

    def _prepare_task_result(self, task: dict, show_full: bool = None):
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

            if show_full is None:
                show_full = self._option_bool(task.get("show_full_context"), default=True)
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
                "show_project_root": self._option_bool(task.get("show_project_root"), default=True),
                "diff_result": diff_result,
            }
        except Exception:
            if vcs and cleanup_needed:
                vcs.cleanup()
            raise

    def _multi_display_path(self, project_result: dict, file_path: str) -> str:
        normalized_path = (file_path or "").replace("\\", "/")
        if self._option_bool(project_result.get("show_project_root"), default=True):
            project_name = project_result.get("project_name", "")
            return f"{project_name}/{normalized_path}" if project_name else normalized_path
        return normalized_path

    def _check_multi_display_path_conflicts(self, project_results: list):
        display_sources = {}
        for item in project_results:
            project_name = item.get("project_name", "")
            for file in item["diff_result"].files:
                display_path = self._multi_display_path(item, file.file_path)
                display_sources.setdefault(display_path, set()).add(project_name)

        conflicts = {
            path: sorted(projects)
            for path, projects in display_sources.items()
            if len(projects) > 1
        }
        if not conflicts:
            return

        project_groups = {}
        for projects in conflicts.values():
            group = tuple(projects)
            project_groups[group] = project_groups.get(group, 0) + 1

        lines = ["多项目报告存在同名展示路径，无法生成。", "", "存在冲突的项目组合："]
        grouped_items = sorted(project_groups.items(), key=lambda item: (-item[1], item[0]))
        for projects, count in grouped_items[:10]:
            lines.append(f"- {' / '.join(projects)}（{count} 个同名路径）")
        if len(grouped_items) > 10:
            lines.append(f"... 还有 {len(grouped_items) - 10} 组项目组合存在冲突")
            lines.append("")
        lines.append("请将以上项目中至少一个任务的“报告树及变更清单使用项目名”改为“是”后重试。")
        raise RuntimeError("\n".join(lines).strip())

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

        try:
            project_name = self._require_project_name()
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return
        self._refresh_output_paths_now()

        report_path = self.report_path_var.get().strip()
        old_export = self.old_export_var.get().strip()
        new_export = self.new_export_var.get().strip()

        if not report_path:
            messagebox.showwarning("提示", "请先选择输出目录")
            return

        if not self._confirm_output_batch():
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

        if not self._confirm_output_batch():
            return
        effective_output_dir = self._effective_output_dir(output_dir)

        self._save_current_exclude_rules_for_current_key()
        self._save_current_config()

        self.old_export_var.set(self._join_display_path(effective_output_dir, "oldVersion"))
        self.new_export_var.set(self._join_display_path(effective_output_dir, "newVersion"))

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
                vcs = SVNMultiVersionVCS(project_path, parse_multi_versions(old_version))
                cleanup_vcs = vcs
            elif vcs_type == "git":
                vcs = GitVCS(project_path)
            else:
                vcs = SVNVCS(project_path)

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
            for idx, task in enumerate(tasks, start=1):
                info(f"多项目任务 {idx}/{len(tasks)}: {task.get('project_name')} {task.get('vcs_type')}")
                self.root.after(0, lambda idx=idx, total=len(tasks), name=task.get("project_name", ""):
                                self.status_var.set(f"正在处理项目 {idx}/{total}: {name}"))
                project_results.append(self._prepare_task_result(task))

            self._check_multi_display_path_conflicts(project_results)

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
        self._refresh_recent_project_values()
        output_dir = self._display_path(os.path.dirname(report_path))
        self.status_var.set(
            f"完成! 共 {summary['total_files']} 个文件变更 "
            f"(+{summary['total_added_lines']}/-{summary['total_deleted_lines']})"
        )
        if messagebox.askyesno("完成", f"比对报告已生成!\n\n"
                                       f"变更文件: {summary['total_files']} 个\n"
                                       f"新增行数: +{summary['total_added_lines']}\n"
                                       f"删除行数: -{summary['total_deleted_lines']}\n\n"
                                       f"输出目录:\n{output_dir}\n\n"
                                       f"是否打开报告?"):
            webbrowser.open(f"file:///{report_path}")

    def _on_multi_complete(self, report_path, summary):
        self.progress.stop()
        self._set_generating(False)
        output_dir = self._display_path(os.path.dirname(report_path))
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
                                       f"输出目录:\n{output_dir}\n\n"
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
        self._remember_recent_project(refresh=False)
        data = {
            "project_path": self.dir_entry.get().strip(),
            "vcs_type": self.vcs_var.get(),
            "project_exclude_rules": self._project_exclude_rules,
            "project_display_options": self._project_display_options,
            "recent_projects": self._recent_projects,
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
