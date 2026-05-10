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

打包时需确保 `templates/` 目录和 `paichu.txt` 与 main.py 在同一目录下。`pyinstaller` 的 `--add-data` 已处理 `templates`，`paichu.txt` 通过 `BASE_DIR` 自动定位。

## 架构

```
main.py                  # tkinter GUI 入口，线程管理，配置持久化
├── vcs/
│   ├── base.py          # BaseVCS 抽象类 + ChangedFile/ChangeType 数据类 + glob 排除匹配
│   ├── git_vcs.py       # GitVCS：git diff --name-status / git show / git log
│   └── svn_vcs.py       # SVNVCS：svn diff --summarize / svn cat / svn log
├── diff_engine.py       # DiffEngine：遍历变更文件，生成 difflib.HtmlDiff 左右对比 HTML
├── report_generator.py  # Jinja2 渲染 templates/report.html → 单文件 HTML
├── file_exporter.py     # 变更文件按目录结构导出到 old/ 和 new/ 目录
└── templates/report.html # HTML 报告模板（文件树 + 左右对比 + 变更清单弹窗）
```

### 数据流

1. `main.py` 收集用户输入（项目路径、VCS 类型、版本号、排除规则、输出路径）
2. 创建 `GitVCS` / `SVNVCS` → 调用 `get_changed_files()` 获取变更文件列表
3. `DiffEngine.generate_diff()` 遍历文件，读取新旧内容，用 `difflib.HtmlDiff.make_table()` 生成 side-by-side HTML
4. `ReportGenerator` 用 Jinja2 渲染模板，将 side-by-side HTML 嵌入 hidden div，JavaScript 读取后交给文件树交互
5. `FileExporter` 按版本导出变更文件到指定目录

### 编码处理

- **SVN cat / 本地文件**：`_decode_bytes()` 先 UTF-8 解码，失败回退 GBK（中文 Windows Java 项目常用）
- **Git 路径**：`_unescape_git_path()` 处理 `core.quotepath` 默认开启时的八进制转义（如 `\347\274\226` → 编译）
- `paichu.txt` 同样是 UTF-8 → GBK 回退解码

### 排除规则

`paichu.txt` 中的 glob 模式在 `base._match_glob()` 中转正则：
- 不含 `/` 的模式（如 `*.class`）自动匹配任意深度
- `**/` → 可选目录前缀 `(.*/)?`
- `**`（末尾）→ `.*`
- 单 `*` → `[^/]*`

### 配置持久化

`compareTool_config.json` 保存项目路径、VCS 类型、排除规则、输出路径。`paichu.txt` 存在时其排除规则优先级高于配置文件。

### Shell 命令依赖

工具通过 `subprocess` 调用 `git` / `svn` 命令行，使用 `cwd=self.project_path` 指定工作目录，无需进入项目目录。
