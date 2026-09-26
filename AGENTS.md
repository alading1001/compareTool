# AGENTS.md

This file is the shared source of truth for AI coding agents working in this repository.

## Overview

代码比对报告工具 — Windows 桌面应用，输入 Git/SVN/文件夹/压缩包路径和版本信息，生成 HTML 差异报告并导出变更文件。通过 PyInstaller 打包成单文件 exe，无需 Python 环境。

## 运行与打包

```bash
# 开发运行
pip install jinja2
python main.py

# 打包成 exe（输出到 dist/CompareTool.exe）
build.bat
```

`build.bat` 优先复用项目内 `.venv`；不存在时优先使用 Python 3.12 创建 `.venv`，并按 `requirements.txt` 补齐构建依赖。打包时需确保 `templates/` 和 `assets/` 目录与 main.py 在同一目录下。PyInstaller 的 `--add-data` 已处理 `templates` 和 `assets`。应用图标使用 `assets/icons/app.ico`，`--icon` 写入 exe 图标，运行时窗口图标也从同一路径加载。使用 `--console` 而非 `--windowed`，确保 git/svn 子进程有终端可用，避免凭据认证弹 GUI 窗口。每次正式打包后必须启动新 EXE 做冒烟验证，不能只以 PyInstaller 返回成功判定可交付。

使用说明书的唯一维护源是 `docs/manual/build_manual.py`，正式 PDF 位于 `docs/CompareTool_使用说明书.pdf`；`dist` 中只放交付副本，`build.bat` 负责复制。界面流程变化时同步源稿和 PDF，按 [说明书维护](docs/manual/README.md) 校验结构、两份字节一致并渲染检查；文档维护依赖不加入应用依赖。

## 架构

```
main.py                  # tkinter GUI 入口，线程管理，配置持久化，UI 防抖
├── vcs/
│   ├── base.py          # BaseVCS 抽象类 + ChangedFile/ChangeType + glob 排除匹配
│   ├── git_vcs.py       # GitVCS：git diff --raw -z --find-renames / git show / git log
│   ├── git_checkout.py  # 隔离 Git 属性/配置快照、微小检出探针和流式文本判定
│   ├── svn_vcs.py       # SVNVCS：svn diff --summarize / svn cat (URL+@peg) / svn log
│   ├── folder_vcs.py    # FolderVCS：先快照两个端点，再用分块字节读取判断差异
│   ├── archive_vcs.py   # ArchiveVCS：解压 zip/tar 到临时目录，委托 FolderVCS 比对
│   └── multi_version_vcs.py # Git/SVN 多版本：历史身份追踪 + 文件级端点快照
├── diff_engine.py       # 遍历变更、编码/格式判定、差异统计及左右 HTML
├── stable_diff.py       # HtmlDiff 递归失败时的完整非递归差异表
├── report_generator.py  # Jinja2 渲染 templates/report.html → 单文件 HTML
├── file_exporter.py     # 变更文件按目录结构导出到 oldVersion/newVersion
├── delivery_instructions.py # 生成上线删除/重命名操作说明
├── logger.py            # 简易日志，仅 warn/error 写文件（info 为空操作），512KB 轮转
├── templates/report.html # 单项目 HTML 报告模板（文件树 + 左右对比 + 变更清单弹窗）
└── templates/multi_report.html # 多项目总报告模板（项目分组文件树 + 左右对比 + 变更清单弹窗）
```

### 数据流

1. `main.py` 收集输入：项目路径、VCS 类型（Git/SVN/文件夹/压缩包/Git多版本/SVN多版本）、旧/新版本号或多版本列表、排除规则、输出目录
2. 根据 VCS 类型创建 `GitVCS` / `SVNVCS` / `FolderVCS` / `ArchiveVCS` / `GitMultiVersionVCS` / `SVNMultiVersionVCS` → `get_changed_files()` 获取变更文件列表
3. `DiffEngine.generate_diff()` 遍历文件，对文本文件生成 side-by-side HTML（标准 `HtmlDiff` 及完整非递归回退）；二进制文件跳过内容只设占位标记；内容完全一致且唯一匹配的删除+新增会合并为重命名
4. `ReportGenerator` 用 Jinja2 渲染模板 → 单文件 HTML
5. `FileExporter` 通过 `export_file_to_path()` 流式写入新旧变更文件，报告、上线说明及 oldVersion/newVersion 成组提交；任一步失败均恢复原输出。暂存物直接创建在用户配置的可信输出根，慢任务提交前复核正式目标，拒绝实际 Windows 名称碰撞。实现或修改报告、端点写入、导出和恢复流程前，必须阅读 [报告与导出完整性合同](docs/report-and-export-contract.md)。

项目名只在能从有效项目目录、新版本文件夹或新版本压缩包推断出真实名称时自动填充。推断不到且用户未手工填写时，生成报告或添加多项目任务应直接提示失败，不使用 `project` 之类的假兜底名称。Git/SVN/Git多版本/SVN多版本模式下，项目名输入框是可编辑下拉框，会按 Git/SVN 家族记忆最近 10 个有效项目；选择最近项目时必须同步回填项目目录和项目名，并触发项目路径变化逻辑清空版本选择和版本列表，避免跨项目复用版本号；多版本只读的“生成结果”仍须显示“文件级首尾端点”。

### 多项目总报告

`main.py` 维护 `multi_tasks` 任务列表，每个任务保存项目名、VCS 类型、路径/版本、排除规则、是否使用项目名、差异展示方式等快照。输出批次名称是当前这次输出的全局字段，启动时默认 `yyyyMMdd`，不按项目记忆、不保存到配置、不写入多项目任务快照。生成多项目总报告时，逐个任务创建对应 VCS、按任务自己的差异展示方式运行 `DiffEngine`，全部成功后统一调用 `ReportGenerator.generate_multi()` 渲染 `templates/multi_report.html`，并用 `FileExporter` 导出到：

```
oldVersion/项目名/...
newVersion/项目名/...
```

若填写了输出批次名称，批次根目录会变成 `输出目录/输出批次名称/`。单项目报告和导出仍生成在批次根；每次多项目生成则必须在批次根下创建独立的 `multi_run_yyyyMMdd_HHmmss_SSS_<8位随机十六进制>/` 运行目录，报告、`上线操作说明.txt`、`oldVersion` 和 `newVersion` 全部位于该运行目录内。随机后缀用于消除同一毫秒并发碰撞；恢复逻辑仍兼容旧的纯时间戳目录。这样历史多项目报告不会引用后一次运行的说明或源码包，也不会与同批次单项目导出互相覆盖。生成前提示的“实际输出目录”必须是本次真正写入的批次根或 multi run 目录。

多项目任务允许混用 Git/SVN/文件夹/压缩包/Git多版本/SVN多版本。任一任务失败时本次生成失败，不跳过项目，也不提交任何项目的新导出目录、新说明文件或新报告。总报告文件名格式为 `multi_compare_report_yyyyMMdd_HHmmss_SSS.html`，并与该次运行的说明和源码包共同保存在独立 multi run 目录。任务列表保存到 `compareTool_config.json`。多项目项目名按 Windows 大小写不敏感规则判重，避免 `Demo` / `demo` 写入同一目录。多项目变更清单是纯文本页面，按新增/修改/格式变化/删除/重命名汇总；每条路径是否带项目名由该任务自己的 `show_project_root` 决定。生成总报告前会检测最终展示路径冲突，若同一展示路径来自多个项目，则失败并提示开启相关任务的项目名展示。`上线操作说明.txt` 中的路径始终带项目名。

### VCS 类型与版本标识

| 类型 | 旧版本标识 | 新版本标识 | 备注 |
|------|-----------|-----------|------|
| Git | commit hash / tag / branch | 同左 | `get_file_content_working` 直接读工作副本文件 |
| SVN | `rNNNNN` 或 `NNNNN` | 同左 | `get_file_content` 使用仓库 URL + peg revision |
| 文件夹 | 旧文件夹路径 | 新文件夹路径 | 生成开始时先把两个目录快照到 CompareTool 专用临时目录；版本标识兼容 `"old"`/`"new"` 和用户输入的实际路径 |
| 压缩包 | 旧压缩包路径 | 新压缩包路径 | 解压到临时目录后委托 `FolderVCS` 比对；支持 `.zip` / `.jar` / `.war` / `.ear` / `.aar` / `.tar` / `.tar.gz` / `.tgz` / `.tar.bz2` / `.tbz2` |
| Git多版本 | 多个 commit hash | `文件级首尾端点` | 每个文件 old 取首次选中变更的第一父提交，new 取末次选中提交 |
| SVN多版本 | 多个 `rNNNNN` 或 `NNNNN` | `文件级首尾端点` | 每个文件 old 取首次选中 revision 前状态，new 取末次选中 revision 后状态 |

### 版本列表交互

Git/SVN/Git多版本/SVN多版本的版本列表只搜索当前已经展示的列表内容，不额外查询仓库。普通 Git/SVN 模式仍由 `GitVCS.get_versions()` / `SVNVCS.get_versions()` 获取 tags/分支/最近 100 条日志或最近 100 条 revision；多版本模式仍由 `get_recent_versions()` 获取当前项目最近 100 条主线提交或相关 revision。

版本列表工具条包含搜索框、清空按钮、填入按钮，以及「隐藏版本列表 / 显示版本列表」切换按钮。隐藏只收起 `Listbox`，不清空 `_version_items`、搜索词或多版本选择状态；重新获取版本列表、切换项目路径或切换 VCS 类型时应重置版本列表状态并自动展开。Git多版本/SVN多版本多选要通过 `_selected_multi_versions` 保留跨搜索过滤、隐藏/显示后的选择，填入时按原始版本列表顺序输出。

### 重命名处理

`ChangeType.RENAMED` 中 `file_path` 表示新路径，`old_path` 表示旧路径。`GitVCS` 使用 `git diff --raw -z --find-renames` 获取 Git 明确识别的重命名和类型变化。普通 SVN、文件夹和压缩包可由 `DiffEngine._merge_exact_renames()` 把内容字节完全一致且唯一匹配的 `DELETED + ADDED` 合并成 `RENAMED`。Git多版本/SVN多版本由端点规划器沿历史追踪文件身份，禁止再对最终删除/新增做内容二次配对。重命名只发生编码/BOM/换行变化时要显示明确说明；排除规则只命中新旧一侧时必须转换为删除或新增。普通 Git 比对遇到 `T` 类型变化必须中止；Git 多版本历史中的 `T` 只用于延续同路径身份，若任一最终选中端点不是普通文件仍必须中止。报告模板必须把 `R` 纳入汇总卡片、文件树标签、过滤器和纯文本变更清单。

### Git/SVN 多版本文件端点

Git 普通/多版本均以用户选择的目录作为项目范围，允许仓库根、子目录和裸仓库。Git 命令在仓库根执行；报告、排除规则、身份规划和导出使用项目相对路径，读取对象和属性时映射回仓库相对路径。跨项目边界的 rename 在历史竞争前降级为本项目一侧的新增或删除，不得混入兄弟项目，也不得重复添加所选子目录前缀。裸仓库以仓库根作为范围，输出仍须位于仓库元数据之外。

Git多版本/SVN多版本使用“文件级首尾端点”语义：选中版本只决定候选文件集合及每个文件的首次/末次选中变更；old 取该文件首次选中变更之前的真实状态，new 取末次选中变更之后的真实状态，只比较最终净结果。不同文件允许来自不同 commit/revision；报告和 oldVersion/newVersion 使用同一端点；newVersion 导出完整文件，但不是某个单一版本的完整项目快照。

- `GitMultiVersionVCS` 只接受当前分支第一父历史的选中提交，合并提交相对第一父提交计算；浅克隆缺少父对象时失败。历史按正常阈值追踪重命名；只有紧凑候选组中的不同逻辑实体实际跨越选中端点时，才运行低阈值第二遍 diff 或隔离 source/target blob 的 Git 原生 rename score，首个命中即 fail closed，不为无关历史提前展开或评分 `D/R × A/R/M` 笛卡尔积。跨提交待定删除也只保存活动区间和目标事件，最终按时序过滤后评分。候选身份跨选中端点且不唯一时仍必须中止。
- `SVNMultiVersionVCS` 解析当前项目 URL 的 `svn log --xml -v`，按 revision 映射项目根/祖先移动前缀，用 `copyfrom-path` 和删除覆盖关系追踪文件身份；同 revision 根移动加子文件改名、嵌套目录移动、子文件移出目录或覆盖已有目标、延迟 copyfrom、移动后删除源祖先都必须保持身份。目录移动同 revision 又从继承后的原后缀复制新文件时，只要原后缀仍存在，就必须视为普通 copy；同一个源分叉到多个目标且自然后缀消失时，安全降级为删除源和新增各目标，不猜测唯一 rename。
- 两种模式均不执行 cherry-pick 或 SVN merge；排除规则必须在每个历史步的身份竞争前生效，命中新旧两侧时跳过，单侧命中时安全降级为新增或删除。导出快照与仓库原始字节快照分离，并直接流式写入磁盘端点后分块比较，不得同时在内存保留四份内容。Git 重命名候选和评分默认不设性能上限，评分 blob 必须流式落盘；候选身份本身无法唯一确认时仍必须中止，不能猜测。Git 多版本历史中的类型变化只延续同路径身份；最终选中端点出现非普通 mode、`svn:special` 或其它非普通文件时，必须在净零过滤前中止。成功或失败后必须清理临时目录。
- 目录剪枝只有在同一条 glob 规则同时覆盖直接和嵌套后代时才允许，不能由不同规则拼接出“看似覆盖”。Git/SVN 生成命令、历史、变更/路径记录、逻辑文件和端点规模默认不设性能型硬上限或生成超时；不能为了磁盘空间预测额外查询全部端点大小，无法可靠预知时直接流式写入并让真实 I/O 失败中止。SVN log/list XML 必须增量解析，不能先构造完整 ElementTree。目录覆盖、移动和排除前缀判断必须使用前缀索引或按路径层级查询，不能让文件数与目录数形成重复笛卡尔扫描。
- 用户切换 Git/SVN/Git多版本/SVN多版本的项目目录时，若路径实际变化，必须清空可选版本输入和版本列表；Git多版本/SVN多版本的只读“生成结果”恢复为“文件级首尾端点”。异步获取版本列表返回时也要校验项目路径和 VCS 类型仍一致。
- 唯一语义规格和验收场景见 [`docs/multi-version-file-endpoints.md`](docs/multi-version-file-endpoints.md)。

### SVN 文件内容获取（重要）

SVN 对**已删除文件**必须使用仓库 URL + peg revision 语法，工作副本路径会失败：

```
正确: svn cat https://svn-server/.../file.txt@240814
错误: svn cat -r 240814 wc_path/file.txt        # E155010: node not found
错误: svn cat -r 240814 https://.../file.txt     # E200009: illegal target (HEAD 中路径不存在)
```

普通 `SVNVCS` 在任务开始时通过同一次 `svn info --xml -r HEAD` 同时固定项目 URL、仓库根、仓库 UUID 和 HEAD peg revision，再把两个输入固定为不晚于该 peg 的数字 revision；summarize、属性、大小、内容与导出必须复用这组身份，不能在工作副本被 `svn switch` 后重新读取 URL。SVN 多版本也必须使用一次原子身份快照，并把历史查询上界固定到该 peg。项目根历史移动时，旧端点 URL 要按 revision 沿 copyfrom 历史解析。路径中的反斜杠需统一转正斜杠（Windows `os.path.relpath` 输出反斜杠）。

Git/SVN 可执行文件路径均自动探测：先查 `shutil.which`，再查 Windows 注册表中的用户/系统 PATH，最后搜常见安装目录。Git 常见目录包括 Git for Windows 的 `cmd/git.exe` / `bin/git.exe`；SVN 常见目录包括 TortoiseSVN、VisualSVN、SlikSVN 等。GUI 不再提供 SVN 可执行文件路径输入框，若最终找不到 `git.exe` / `svn.exe`，应提示用户安装 Git for Windows 或 SVN 命令行工具（TortoiseSVN 需勾选 command line client tools）。

### 压缩包比对

`ArchiveVCS` 通过 `vcs.temp_storage.create_temp_dir()` 将两个压缩包解压到 CompareTool 专用临时目录，然后委托 `FolderVCS` 做文件遍历和内容比对。支持的格式：

- `.zip` / `.jar` / `.war` / `.ear` / `.aar` — `zipfile` 标准库，含 ZIP 文件名 GBK 编码修正
- `.tar` / `.tar.gz` / `.tgz` / `.tar.bz2` / `.tbz2` — `tarfile` 标准库

**ZIP 文件名编码修正**：Windows 中文环境创建的 zip 文件名通常用 GBK 编码而不设 UTF-8 标志位（`flag_bits & 0x800 == 0`）。`_fix_zip_filename()` 将 `ZipInfo.filename` 反向编码为 CP437 原始字节，再按 GBK 解码为正确的中文文件名。

**安全解压与临时目录清理**：构造时先同时捕获并持续持有两个源归档的稳定普通文件句柄，预检和实际解压只读取这些句柄；Windows `deny_writes` 句柄由内核阻止写入/删除，结束前只复核路径与句柄身份，不再额外前后整包哈希；POSIX advisory lock 不能约束不配合的写入方，仍以流式 SHA-256 复核内容。不为源归档额外复制一份完整副本。源归档和文件夹快照不设性能型固定大小/文件数上限；同一文件夹作为新旧端点时只稳定捕获一次并正常生成零变更结果，父子目录端点仍各自独立快照；文件夹只复制实际变更端点但仍复核整个源树，归档按实际展开字节选择有空间的候选临时根。ZIP/TAR 每个成员都必须先通过临时目录边界和 Windows 文件名校验，拒绝父目录穿越、绝对/盘符路径、ADS、符号链接、硬链接和设备等特殊成员；不得直接使用无过滤的 `extractall()`。合法的字面 `ABC~1.TXT` 名称不得仅凭外形拒绝，落盘时逐级核对真实目录项并排他创建文件，只拒绝实际发生的 Windows 名称/短别名碰撞。压缩包安全边界仍默认限制 100,000 个成员、单成员 2 GiB、累计展开 10 GiB 和 1000:1 压缩比；TAR PAX/GNU 元数据记录、字段和字节以及 ZIP 中央目录字节默认不设额外性能型固定上限，显式策略仍可注入。ZIP 在构造 `ZipFile` 前解析 EOCD/ZIP64 与中央目录，确保成员数上限不会生效过晚。临时目录带所有权 sidecar，正常退出清理，启动创建新临时目录时只回收超过 7 天、原进程已不存在且标记有效的专用遗留目录；Windows PID 探测复用 stage 所有权的保守句柄实现。文件夹/多版本端点快照也必须拒绝符号链接和联接点，避免解引用读取根外内容。

文件夹和归档的预捕获路径与读取时已打开句柄必须比较稳定文件 ID：Windows 使用卷序列号与 FileId，其它平台使用设备号/inode；不能只比较 handle 的大小和 mtime，否则同大小同时间戳替换可以混入未捕获内容。

**排除规则转发**：`ArchiveVCS` 覆写 `set_exclude_patterns()`，将规则同步传给内部 `FolderVCS`，否则排除规则不会生效。

**比较根目录**：压缩包模式提供默认关闭的「忽略最外层单一文件夹」选项，传入 `ArchiveVCS(ignore_single_root=True)`。安全解压完成后，两端分别必须恰有一个真实顶层目录且无其它顶层项；只进入一层，不递归剥离、不按排除规则筛选顶层、不猜测目录对应关系。不满足时失败并提示取消勾选或手工选择目录。内部 `FolderVCS` 根和归档权限元数据同时重定位，报告、排除规则、摘要、读取、导出、重命名及目录删除说明使用同一内层相对路径。所有权临时根保持不变以便完整清理。`comparison_note` 记录两侧根名称，单/多项目报告与上线说明均展示。GUI 的 `ignore_archive_root` 按新压缩包路径记忆，并保存到多项目任务快照；旧配置缺字段默认关闭。

**报告路径修正**：`main.py` 在生成 diff 后，对压缩包模式将 `diff_result.project_path` 覆写为压缩包文件名（而非临时目录），避免报告头部泄露临时路径。

**版本标识翻译**：`ArchiveVCS._to_folder_ver()` 将外部版本标识（zip 路径）映射为 `"old"`/`"new"` 后再委托给 `FolderVCS`。`FolderVCS._resolve_version_dir()` 只识别 `"old"`/`"new"` 和临时目录路径，不识别 zip 路径，必须经翻译层转换。内容、大小、摘要和流式导出均使用此翻译。

### 差异展示与二进制导出

默认展示全部内容，可选前后各 3 行上下文。正常输入默认不设性能型规模上限；`HtmlDiff` 递归失败时保留完整逐行差异，未知字节无损标记，F/R 语义与原始导出字节必须保留。二进制仅跳过内容展示，仍完整流式导出。具体编码、拆行、统计、manifest、写入与事务约束以 [报告与导出完整性合同](docs/report-and-export-contract.md) 为准。

### 排除规则

内置默认排除规则定义在 `main.py` 的 `DEFAULT_EXCLUDE_RULES`。界面中的 glob 模式在 `base._match_glob()` 中转正则：
- 不含 `/` 的模式（如 `*.class`）自动匹配任意深度 → 添加 `**/` 前缀
- `**/` → 可选目录前缀 `(.*/)?`
- `**`（末尾）→ `.*`
- 单 `*` → `[^/]*`

默认模板偏通用，只排除 VCS 元数据、Java/Python/Node 常见构建产物、日志/临时目录、IDE 元数据和系统文件。项目配置、脚本、文档类文件（如 `README.md`、`gradlew`、`settings.gradle`、`gradle.properties`）不应默认排除，应由用户按项目自行添加。

### 换行符处理

导出字节必须符合固定端点的 Git 属性及配置或 SVN `svn:eol-style`；文件夹保留原字节。Git 属性由隔离的原生探针判定，不执行外部检出程序；无法可靠复现的有效转换仍须中止。普通和多版本模式复用同一规则，完整支持与拒绝边界见 [报告与导出完整性合同](docs/report-and-export-contract.md#换行符处理)。

### 编码

- **SVN 子进程输出**：`_run()` 读取原始字节，通过 `_decode_bytes()` 自动探测编码（UTF-8 → GBK → 回退）。影响 svn log、svn diff、svn info 等所有命令输出
- **SVN cat / 本地文件**：同样走 `_decode_bytes()`（UTF-8 → GBK）
- **Git 路径**：`_unescape_git_path()` 解码 `core.quotepath` 的八进制和 C 风格转义（包括 tab、换行、引号和反斜杠）。

### Shell 依赖

所有 VCS 操作通过 `subprocess` 调用 `git` / `svn` 命令行。SVN 使用项目目录；Git 仓库命令使用固定仓库根，并显式映射项目相对路径。隔离检出探针只读取固定端点属性，不执行用户配置中的外部转换程序。

### 配置持久化

`compareTool_config.json` 保存项目路径、VCS 类型、输出路径、多项目任务列表、`recent_projects`、`project_exclude_rules` 和 `project_display_options`。配置必须先写同目录临时文件并 `os.replace()` 原子替换，避免进程中断破坏已有任务；序列化、写入或替换失败必须向用户报错，不得显示假成功。若现有配置本身无法解析，程序可用默认值启动，但必须告警并阻止覆盖原损坏文件。启动时会规范化 `multi_tasks` schema，损坏、缺字段、VCS 类型未知或同名的任务记录应记录警告并忽略，不能阻止窗口启动；旧多版本任务的 `new_version` 会迁移为“文件级首尾端点”。项目级配置按规范化绝对路径保存：Git/SVN/Git多版本/SVN多版本用项目目录，文件夹用新版本文件夹，压缩包用新版本压缩包完整文件路径。最近项目列表按 Git/SVN 家族分组，每组最多保留最近 10 个有效项目，旧配置没有 `recent_projects` 时应兼容为空并可由当前有效项目回填。新路径没有专属排除规则时，使用 `main.py` 内置默认模板；旧版全局 `exclude_rules` 不再作为默认模板来源。多项目任务添加/更新时保存排除规则和显示选项快照，后续项目默认配置变化不会偷偷影响已添加任务。输出批次名称不持久化，每次启动默认当天日期。

## 第一批性能优化（2026-09-24）

- `stable_diff.prefer_stable_diff()` 对较大的替换段提前选择完整非递归渲染；64 行仅是算法切换点，不是输入或报告上限。小修改保留原 HtmlDiff 对齐；精确行数统计和格式变化语义不变。测试应核对完整可见内容，不应要求必须调用某一种渲染器。
- 非递归渲染先剥离相同前后缀再做行匹配，避免重复边界形成平方级匹配；完整模式仍输出全部边界行，上下文模式保留原始行号及所选上下文。相关回归见 `tests/test_rendering_performance_regressions.py`，重复行及递归模板内存基准已纳入 `tools/benchmark_performance.py`。
- `vcs/git_batch.py` 在每个 GitVCS 生命周期内复用 `git cat-file --batch`，原始字节读取和导出均可使用。导出按 1 MiB 分块，严格校验响应类型、长度及结束符，协议异常或写入失败时关闭通道；空文件正常成功，缺失对象不能伪装成空文件。
- 显式命令超时或无法由换行协议表示的对象路径仍使用原来的 `git show` 路径。Git 属性、检出探针、换行转换、版本固定和事务检查保持原规则，不执行外部 filter。
- `GitVCS.cleanup()` 必须关闭批量读取进程。不要为了重用原始字节而跳过实际检出转换。
- 回归测试见 `tests/test_performance_first_batch.py`；可重复基准见 `tools/benchmark_performance.py`，至少重复三次，并验证报告行内容及导出字节。基准的 Git 用时不包括最终事务提交，不能宣传为完整上线流程耗时。
- 测试桩的 `_tmp_root` 必须是测试独占的临时目录，不能设为 `os.getcwd()`；该对象析构时会清理目录。完整回归建议在源码副本运行，避免错误测试夹具影响开发工作区。

## Windows 文件夹扫描性能与安全边界（2026-09-25）

- `path_safety.is_link_or_junction()` 在当前 Windows Python 上应复用一次 `lstat` 的 `st_reparse_tag`，不要重新串行调用 `islink`、`isjunction`、`lstat`；旧环境无 `st_reparse_tag` 时保留兼容回退。
- `open_regular_file_no_links()` 在 Windows 用 `FILE_FLAG_OPEN_REPARSE_POINT` 打开最终路径组件，再按句柄检查 reparse tag 与 FileId。不要恢复“先查路径、再普通打开”的 TOCTOU 窗口。
- `regular_file_path_identity()` 可复用同一次安全打开得到的初始句柄身份；真正读取内容的调用方仍须在读取前后重新检查句柄身份，并在需要时重新核对路径身份。
- `FolderVCS` 快照捕获/最终验证可把普通文件的 leaf-link 检查交给紧随其后的安全身份打开；目录在遍历前仍必须检查 junction/symlink，防止 `os.walk` 进入根外。
- `FolderVCS._resolve_file_path()` 只缓存比较根的 `realpath`；缓存键必须保留路径大小写，不能用 `normcase` 合并，因为 Windows 目录可启用大小写敏感。目标文件的 `realpath` 必须每次重新计算，不能缓存，否则运行期间插入中间 junction/symlink 会绕过越界检测。
- 回归见 `tests/test_folder_scan_optimization.py`；扫描基准见 `tools/benchmark_folder_scan.py`。性能改动不得删除源树最终复核、FileId 检查或失败即中止语义。

根路径缓存键必须保留规范化绝对路径的大小写，不能用 `normcase` 合并大小写敏感目录。补充回归见 `tests/test_folder_scan_edge_cases.py`；本轮完整测试和交替基准结果见 `docs/performance-folder-scan.md`。基准只创建独占随机临时目录，不清理固定名称的已有路径。

## 正式版身份与任务观测（2026-09-26）

- `app_version.py` 定义日历版本；正式构建使用 `tools/build_release.py`（由 build.bat 调用），将时间、父提交、dirty 状态和源码 SHA-256 写入临时 build_info.json 并嵌入。运行时不可猜测构建号；源码为 source，缺失元数据为 unknown。
- 只发布 `dist/CompareTool.exe`，旧构建归档。保留 `CompareTool_build.json` 以核对 EXE 和源码指纹。打包返回成功后仍需检查真实窗口及标题版本。
- `task_progress.py` 通过 ContextVar 隔离任务，观测装饰器不改变比较、过滤、字节导出或恢复策略。阶段包含时间不能直接相加，汇总同时提供 exclusive_seconds。
- `ui_progress.py` 仅由主线程每 200ms 轮询最新状态槽；不为每个文件排队 Tk 回调，不逐文件写日志。不知道总量时不编造百分比。后台清理与任务记录完成后才发完成通知。
- 耗时日志失败不得导致业务比较失败；只记录计数、耗时和构建身份，不写代码正文。日志目录不得位于输入树中，必要时选择独立用户目录。保留最近 50 组已结束日志，中断现场不自动清除。
- `tests/test_task_progress.py` 和 `tests/test_release_identity.py` 覆盖观测独立性及身份；维护界面时同步说明书源稿和两个 PDF。完整验证记录见 docs/release-polish.md。

## 包内递归审查（2026-09-26）

- `archive_report.py` 仅向 `FileDiff.archive_details` 附加报告子树；绝不能将成员塞入主交付清单或让包内删除进入宿主删除指令。不改写、重压缩任何交付包。
- 开关 `recursive_archives` 默认关闭，按来源保存并由多项目任务快照持有；首版只允许 folder/archive。旧配置缺字段保持原行为。
- 相同包字节不继续展开；变化包复用安全解压器。包内正文、格式、模式属性和仅打包差异分开说明；CLASS 不反编译。相同子包不等于已验证其格式有效。
- 嵌套展开阶段在整份报告中共享 ArchiveReportBudget（多项目不重置）：8 层、100,000 成员、10 GiB；既检查声明大小也累计实际写出字节，超限明确中止，不做成功的部分报告。
- `templates/archive_details.html` 使用惰性 DOM 子树和局部差异导航，普通文件导航不变；包名和成员名必须转义，不能拼入可执行脚本或宿主路径。
- 包内模板通过递归 `include` 逐片段输出，不能用返回整棵子树字符串的 Jinja 宏替代；浏览器惰性展开不等于生成端流式写出。单/多项目均须覆盖完整成员内容与最大输出块的回归。
- 详细规则见 docs/nested-archive-report.md。外层导出文件清单、原始字节、主统计和上线说明必须在启用前后核对一致。
