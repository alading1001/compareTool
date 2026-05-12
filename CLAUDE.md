# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

代码比对报告工具 — Windows 桌面应用，输入 Git/SVN 项目路径和两个版本号，生成 HTML 差异报告并导出变更文件。通过 PyInstaller 打包成单文件 exe，无需 Python 环境。

## 运行与打包

```bash
# 开发运行
pip install jinja2
python main.py

# 打包成 exe（输出到 dist/CompareTool.exe）
build.bat
```

打包时需确保 `templates/` 目录和 `paichu.txt` 与 main.py 在同一目录下。PyInstaller 的 `--add-data` 已处理 `templates`，`paichu.txt` 通过 `BASE_DIR` 自动定位。使用 `--console` 而非 `--windowed`，确保 git/svn 子进程有终端可用，避免凭据认证弹 GUI 窗口。

## 架构

```
main.py                  # tkinter GUI 入口，线程管理，配置持久化，UI 防抖
├── vcs/
│   ├── base.py          # BaseVCS 抽象类 + ChangedFile/ChangeType + glob 排除匹配
│   ├── git_vcs.py       # GitVCS：git diff --name-status / git show / git log
│   ├── svn_vcs.py       # SVNVCS：svn diff --summarize / svn cat (URL+@peg) / svn log
│   └── folder_vcs.py    # FolderVCS：两个文件夹直接比对，filecmp.cmp 判断差异
├── diff_engine.py       # DiffEngine：遍历变更文件，difflib.HtmlDiff.make_table()
├── report_generator.py  # Jinja2 渲染 templates/report.html → 单文件 HTML
├── file_exporter.py     # 变更文件按目录结构导出到 old/ 和 new/ 目录
├── logger.py            # 简易日志，仅 warn/error 写文件（info 为空操作），512KB 轮转
└── templates/report.html # HTML 报告模板（文件树 + 左右对比 + 变更清单弹窗）
```

### 数据流

1. `main.py` 收集输入：项目路径、VCS 类型（Git/SVN/文件夹）、旧/新版本号、排除规则、输出目录
2. 根据 VCS 类型创建 `GitVCS` / `SVNVCS` / `FolderVCS` → `get_changed_files()` 获取变更文件列表
3. `DiffEngine.generate_diff()` 遍历文件，对文本文件用 `difflib.HtmlDiff.make_table()` 生成 side-by-side HTML；二进制文件跳过内容只设占位标记
4. `ReportGenerator` 用 Jinja2 渲染模板 → 单文件 HTML
5. `FileExporter` 导出变更文件：统一通过 `vcs.get_file_content_bytes()` 读取原始字节，以 `wb` 模式写入，保留原始编码。仅在无法获取原始字节时回退到文本内容（UTF-8）

### VCS 类型与版本标识

| 类型 | 旧版本标识 | 新版本标识 | 备注 |
|------|-----------|-----------|------|
| Git | commit hash / tag / branch | 同左 | `get_file_content_working` 直接读工作副本文件 |
| SVN | `rNNNNN` 或 `NNNNN` | 同左 | `get_file_content` 使用仓库 URL + peg revision |
| 文件夹 | 旧文件夹路径 | 新文件夹路径 | 版本标识即为文件夹路径，`_resolve_version_dir()` 同时兼容 `"old"`/`"new"` 和实际路径 |

### SVN 文件内容获取（重要）

SVN 对**已删除文件**必须使用仓库 URL + peg revision 语法，工作副本路径会失败：

```
正确: svn cat https://svn-server/.../file.txt@240814
错误: svn cat -r 240814 wc_path/file.txt        # E155010: node not found
错误: svn cat -r 240814 https://.../file.txt     # E200009: illegal target (HEAD 中路径不存在)
```

`SVNVCS._repo_url` 通过 `svn info --show-item url` 懒加载缓存仓库根 URL，`get_file_content` / `get_file_content_bytes` 拼接 `{url}/{path}@{rev}` 获取内容。路径中的反斜杠需转正斜杠（Windows `os.path.relpath` 输出反斜杠）。

SVN 可执行文件路径通过 `_find_svn()` 自动探测：先查 `shutil.which`，再查 Windows 注册表中的用户/系统 PATH，最后搜常见安装目录（TortoiseSVN、VisualSVN 等）。GUI 中 SVN 路径输入框仅在 SVN 模式下显示，留空自动探测。

### 差异展示

`DiffEngine.__init__` 接收 `show_full_context` 参数（由 GUI 单选按钮控制）。`True` 时展示文件全部行（`context=False`），`False` 时仅展示差异上下文（`context=True, numlines=3`）。默认为全部内容。

### 二进制文件处理

- `DiffEngine.BINARY_EXTS` 定义二进制扩展名集合（`.jar`, `.war`, `.class`, `.dll` 等）
- `DiffEngine._diff_file()` 对二进制文件提前返回，不读内容，`side_by_side_html` 设为占位提示
- 导出时所有文件（含二进制）统一调用 `vcs.get_file_content_bytes()` 读取原始字节，以 `wb` 模式写入，保留原始编码
- 各 VCS 实现覆写 `get_file_content_bytes()`：Git 用 `git show` 返回原始 stdout，SVN 用 `svn cat` URL 返回原始字节，Folder 直接 `open(full_path, "rb")`

### 排除规则

`paichu.txt` 中的 glob 模式在 `base._match_glob()` 中转正则：
- 不含 `/` 的模式（如 `*.class`）自动匹配任意深度 → 添加 `**/` 前缀
- `**/` → 可选目录前缀 `(.*/)?`
- `**`（末尾）→ `.*`
- 单 `*` → `[^/]*`

### 编码

- **SVN 子进程输出**：`_run()` 读取原始字节，通过 `_decode_bytes()` 自动探测编码（UTF-8 → GBK → 回退）。影响 svn log、svn diff、svn info 等所有命令输出
- **SVN cat / 本地文件**：同样走 `_decode_bytes()`（UTF-8 → GBK）
- **Git 路径**：`_unescape_git_path()` 解码 `core.quotepath` 八进制转义（`\347\274\226` → 编）
- **paichu.txt**：UTF-8 → GBK 回退

### Shell 依赖

所有 VCS 操作通过 `subprocess` 调用 `git` / `svn` 命令行，`cwd=self.project_path`。

### 配置持久化

`compareTool_config.json` 保存项目路径、VCS 类型、SVN 路径、排除规则、输出路径。排除规则优先级：用户保存的配置 > `paichu.txt` > 内置默认值。
