import json
import os
import sys
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# PyInstaller 打包后的资源路径处理
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    CONFIG_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CONFIG_DIR = BASE_DIR

CONFIG_FILE = os.path.join(CONFIG_DIR, "compareTool_config.json")

from vcs.git_vcs import GitVCS
from vcs.svn_vcs import SVNVCS
from vcs.folder_vcs import FolderVCS
from diff_engine import DiffEngine
from report_generator import ReportGenerator
from file_exporter import FileExporter


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
        self.root.geometry("760x770")
        self.root.resizable(True, True)
        self.root.minsize(600, 700)

        icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            self.root.iconbitmap(icon_path)

        self._default_output = os.path.join(os.path.expanduser("~"), "Desktop")
        self._config = _load_config()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ========== UI 构建 ==========

    def _build_ui(self):
        main = ttk.Frame(self.root, padding="16")
        main.pack(fill=tk.BOTH, expand=True)

        # ── 项目目录 ──
        self.project_label = ttk.Label(main, text="项目目录:", font=("", 10))
        self.project_label.grid(row=0, column=0, sticky=tk.W, pady=(0, 2))
        self.project_dir_frame = ttk.Frame(main)
        self.project_dir_frame.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        self.dir_entry = ttk.Entry(self.project_dir_frame)
        self.dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.dir_entry.bind("<KeyRelease>", lambda e: self._update_output_paths())
        self.dir_entry.bind("<FocusOut>", lambda e: self._update_output_paths())
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
        ttk.Radiobutton(vcs_frame, text="文件夹", variable=self.vcs_var, value="folder").pack(side=tk.LEFT)

        # ── 排除规则 ──
        ttk.Label(main, text="排除规则 (每行一个，支持 * 和 ** 通配符):", font=("", 10)).grid(row=4, column=0, sticky=tk.W, pady=(0, 4))
        exclude_frame = ttk.Frame(main)
        exclude_frame.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        self.exclude_text = tk.Text(exclude_frame, height=4, wrap=tk.NONE)
        self.exclude_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 优先从 paichu.txt 读取排除规则，找不到再用历史配置
        paichu_path = os.path.join(BASE_DIR, "paichu.txt")
        if os.path.exists(paichu_path):
            with open(paichu_path, "rb") as pf:
                raw = pf.read()
            for enc in ("utf-8", "gbk"):
                try:
                    default_excludes = raw.decode(enc).strip()
                    break
                except UnicodeDecodeError:
                    continue
            else:
                default_excludes = raw.decode("utf-8", errors="replace").strip()
        else:
            default_excludes = self._config.get("exclude_rules", (
                "*.class\n*.war\n*.ear\n"
                "target/**\nbuild/**\nbin/**\ndist/**\n"
                ".git/**\n.svn/**\n"
                ".idea/**\n.settings/**\n.project\n.classpath\n"
                "node_modules/**\n**/__pycache__/**\n*.pyc\n"
                ".DS_Store\nThumbs.db"
            ))
        self.exclude_text.insert("1.0", default_excludes)
        exclude_scroll = ttk.Scrollbar(exclude_frame, orient=tk.VERTICAL, command=self.exclude_text.yview)
        exclude_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.exclude_text.config(yscrollcommand=exclude_scroll.set)

        # ── 版本 / 文件夹选择 ──
        self.old_version_var = tk.StringVar()
        self.new_version_var = tk.StringVar()
        self.new_version_var.trace_add("write", lambda *_: self._update_output_paths())

        # 旧版本标签（动态切换）
        self.old_label = ttk.Label(main, text="旧版本 (改动前):", font=("", 10))
        self.old_label.grid(row=6, column=0, sticky=tk.W, pady=(0, 2))

        old_frame = ttk.Frame(main)
        old_frame.grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self.old_entry = ttk.Entry(old_frame, textvariable=self.old_version_var)
        self.old_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.old_vcs_btn = ttk.Button(old_frame, text="获取版本列表", command=lambda: self._fetch_versions("old"))
        self.old_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.old_folder_btn = ttk.Button(old_frame, text="浏览...", command=lambda: self._browse_dir(self.old_version_var))
        # 文件夹浏览按钮初始隐藏

        # 新版本标签（动态切换）
        self.new_label = ttk.Label(main, text="新版本 (改动后):", font=("", 10))
        self.new_label.grid(row=8, column=0, sticky=tk.W, pady=(0, 2))

        new_frame = ttk.Frame(main)
        new_frame.grid(row=9, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self.new_entry = ttk.Entry(new_frame, textvariable=self.new_version_var)
        self.new_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.new_vcs_btn = ttk.Button(new_frame, text="获取版本列表", command=lambda: self._fetch_versions("new"))
        self.new_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.new_folder_btn = ttk.Button(new_frame, text="浏览...", command=lambda: self._browse_dir(self.new_version_var))
        # 文件夹浏览按钮初始隐藏

        # 版本列表 + 填入按钮（仅 Git/SVN 模式使用）
        self.version_listbox = tk.Listbox(main, height=7, exportselection=False)
        self.version_listbox.grid(row=10, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        self.version_listbox.grid_remove()
        self.version_listbox.bind("<ButtonRelease-1>", self._on_version_click)

        fill_btn_frame = ttk.Frame(main)
        fill_btn_frame.grid(row=11, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        fill_btn_frame.grid_remove()
        self.fill_btn_frame = fill_btn_frame
        self.fill_target_label = ttk.Label(fill_btn_frame, text="", font=("", 9))
        self.fill_target_label.pack(side=tk.LEFT)
        ttk.Button(fill_btn_frame, text="← 填入选中版本", command=self._fill_selected_version).pack(side=tk.RIGHT)

        self._version_target = "old"

        # 根据初始 vcs_var 切换 UI
        self._on_vcs_changed()

        # ── 输出路径 ──
        ttk.Label(main, text="输出路径设置:", font=("", 10, "bold")).grid(row=12, column=0, sticky=tk.W, pady=(10, 4))

        ttk.Label(main, text="输出目录:").grid(row=13, column=0, sticky=tk.W)
        output_dir_frame = ttk.Frame(main)
        output_dir_frame.grid(row=14, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.output_dir_var = tk.StringVar(value=self._config.get("output_dir", ""))
        self.output_dir_var.trace_add("write", lambda *_: self._update_output_paths())
        ttk.Entry(output_dir_frame, textvariable=self.output_dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(output_dir_frame, text="浏览...", command=lambda: self._browse_dir(self.output_dir_var)).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(main, text="比对报告保存到 (自动生成):").grid(row=15, column=0, sticky=tk.W)
        report_frame = ttk.Frame(main)
        report_frame.grid(row=16, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.report_path_var = tk.StringVar()
        ttk.Entry(report_frame, textvariable=self.report_path_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(main, text="旧版本变更文件导出到 (自动生成):").grid(row=17, column=0, sticky=tk.W)
        old_export_frame = ttk.Frame(main)
        old_export_frame.grid(row=18, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        self.old_export_var = tk.StringVar()
        ttk.Entry(old_export_frame, textvariable=self.old_export_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(main, text="新版本变更文件导出到 (自动生成):").grid(row=19, column=0, sticky=tk.W)
        new_export_frame = ttk.Frame(main)
        new_export_frame.grid(row=20, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        self.new_export_var = tk.StringVar()
        ttk.Entry(new_export_frame, textvariable=self.new_export_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── 底部 ──
        bottom_frame = ttk.Frame(main)
        bottom_frame.grid(row=21, column=0, columnspan=3, sticky=tk.EW, pady=(6, 0))

        self.progress = ttk.Progressbar(bottom_frame, mode="indeterminate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))

        self.generate_btn = ttk.Button(bottom_frame, text="生成比对报告", command=self._generate)
        self.generate_btn.pack(side=tk.RIGHT)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom_frame, textvariable=self.status_var, font=("", 9)).pack(side=tk.RIGHT, padx=(0, 16))

        main.columnconfigure(0, weight=1)

        # 初始化输出路径（有缓存值时自动填充）
        self._update_output_paths()

    # ========== 界面交互 ==========

    def _update_output_paths(self):
        """根据输出目录和项目名自动计算三条路径"""
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            self.report_path_var.set("")
            self.old_export_var.set("")
            self.new_export_var.set("")
            return

        # 计算项目名
        project_path = self.dir_entry.get().strip()
        is_folder = self.vcs_var.get() == "folder"
        if is_folder:
            new_folder = self.new_version_var.get().strip()
            project_name = os.path.basename(os.path.normpath(new_folder)) if new_folder else "project"
        else:
            project_name = os.path.basename(os.path.normpath(project_path)) if project_path else "project"

        self.report_path_var.set(os.path.join(output_dir, f"{project_name}_diff.html"))
        self.old_export_var.set(os.path.join(output_dir, "oldVersion"))
        self.new_export_var.set(os.path.join(output_dir, "newVersion"))

    def _on_vcs_changed(self):
        """VCS 类型切换时更新界面"""
        is_folder = self.vcs_var.get() == "folder"

        # 清空旧/新版本输入
        self.old_version_var.set("")
        self.new_version_var.set("")

        if is_folder:
            # 隐藏项目目录
            self.project_label.grid_remove()
            self.project_dir_frame.grid_remove()
            self.old_label.config(text="旧版本文件夹:")
            self.new_label.config(text="新版本文件夹:")
            self.old_vcs_btn.pack_forget()
            self.old_folder_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.new_vcs_btn.pack_forget()
            self.new_folder_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.version_listbox.grid_remove()
            self.fill_btn_frame.grid_remove()
        else:
            # 显示项目目录
            self.project_label.grid()
            self.project_dir_frame.grid()
            self.old_label.config(text="旧版本 (改动前):")
            self.new_label.config(text="新版本 (改动后):")
            self.old_folder_btn.pack_forget()
            self.old_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.new_folder_btn.pack_forget()
            self.new_vcs_btn.pack(side=tk.LEFT, padx=(6, 0))
            self.version_listbox.grid_remove()
            self.fill_btn_frame.grid_remove()

    def _browse_project(self):
        path = filedialog.askdirectory(title="选择项目目录")
        if path:
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, path)
            self._update_output_paths()

    def _browse_dir(self, var):
        path = filedialog.askdirectory(title="选择保存目录")
        if path:
            var.set(path)

    def _browse_save_file(self, var, desc, ext):
        path = filedialog.asksaveasfilename(title=f"保存{desc}", filetypes=[(desc, ext)], defaultextension=ext)
        if path:
            var.set(path)

    # ========== 版本列表获取 ==========

    def _fetch_versions(self, target="old"):
        self._version_target = target
        project_path = self.dir_entry.get().strip()
        if not project_path:
            messagebox.showwarning("提示", "请先选择项目目录")
            return

        vcs_type = self.vcs_var.get()

        self.version_listbox.delete(0, tk.END)
        self.version_listbox.insert(tk.END, "正在获取版本列表，请稍候...")
        self.version_listbox.grid()
        self.fill_btn_frame.grid()
        self.fill_target_label.config(
            text="将填入: " + ("旧版本" if target == "old" else "新版本")
        )
        self.status_var.set("获取版本列表中...")

        thread = threading.Thread(target=self._do_fetch_versions, args=(project_path, vcs_type), daemon=True)
        thread.start()

    def _do_fetch_versions(self, project_path, vcs_type):
        try:
            if vcs_type == "git":
                vcs = GitVCS(project_path)
            else:
                vcs = SVNVCS(project_path)

            versions = vcs.get_versions()

            def update_ui():
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
            self.root.after(0, lambda: messagebox.showerror("错误", f"获取版本列表失败:\n{e}"))
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
        item = self.version_listbox.get(sel[0])
        if item.startswith("──") or item.startswith("(") or item.startswith("正在"):
            return
        version = item.split(" ")[0] if " " in item else item
        if self._version_target == "new":
            self.new_version_var.set(version)
        else:
            self.old_version_var.set(version)

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

    def _generate(self):
        project_path = self.dir_entry.get().strip()
        old_version = self.old_version_var.get().strip()
        new_version = self.new_version_var.get().strip()
        vcs_type = self.vcs_var.get()
        is_folder = vcs_type == "folder"

        if is_folder:
            # 文件夹模式：验证两个文件夹路径
            if not old_version or not os.path.isdir(old_version):
                messagebox.showwarning("提示", "旧版本文件夹不存在，请选择有效的文件夹")
                return
            if not new_version or not os.path.isdir(new_version):
                messagebox.showwarning("提示", "新版本文件夹不存在，请选择有效的文件夹")
                return
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

        project_name = os.path.basename(os.path.normpath(
            new_version if is_folder else project_path))
        if not self._check_overwrite(project_name):
            return

        self.generate_btn.config(state=tk.DISABLED)
        self.progress.start()
        self.status_var.set("正在生成比对报告...")

        thread = threading.Thread(target=self._do_generate, args=(
            project_path, vcs_type, old_version, new_version
        ), daemon=True)
        thread.start()

    def _do_generate(self, project_path, vcs_type, old_version, new_version):
        try:
            if vcs_type == "folder":
                vcs = FolderVCS(old_version, new_version)
            elif vcs_type == "git":
                vcs = GitVCS(project_path)
            else:
                vcs = SVNVCS(project_path)

            exclude_text = self.exclude_text.get("1.0", tk.END).strip()
            if exclude_text:
                vcs.set_exclude_patterns(exclude_text.split("\n"))

            if vcs_type != "folder":
                if not vcs.check_version_exists(old_version):
                    self._show_error(f"旧版本不存在: {old_version}")
                    return
                if not vcs.check_version_exists(new_version):
                    self._show_error(f"新版本不存在: {new_version}")
                    return

            engine = DiffEngine(vcs)
            diff_result = engine.generate_diff(old_version, new_version)

            report_path = self.report_path_var.get().strip()
            template_dir = os.path.join(BASE_DIR, "templates")
            report_gen = ReportGenerator(template_dir)
            report_gen.generate(diff_result, report_path)

            old_export = self.old_export_var.get().strip()
            new_export = self.new_export_var.get().strip()
            exporter = FileExporter(diff_result, vcs)
            project_name = diff_result.project_name
            exporter.export(old_export, new_export, project_name=project_name)

            # 保存配置
            self._save_current_config()

            summary = diff_result.summary
            self.root.after(0, lambda: self._on_complete(report_path, summary))

        except Exception as e:
            self.root.after(0, lambda: self._show_error(str(e)))

    def _on_complete(self, report_path, summary):
        self.progress.stop()
        self.generate_btn.config(state=tk.NORMAL)
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

    def _show_error(self, msg):
        self.progress.stop()
        self.generate_btn.config(state=tk.NORMAL)
        self.status_var.set("出错")
        messagebox.showerror("错误", msg)

    def _save_current_config(self):
        """保存当前界面配置到文件"""
        data = {
            "project_path": self.dir_entry.get().strip(),
            "vcs_type": self.vcs_var.get(),
            "exclude_rules": self.exclude_text.get("1.0", tk.END).strip(),
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
