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

## 架构

```
main.py                  # tkinter GUI 入口，线程管理，配置持久化，UI 防抖
├── vcs/
│   ├── base.py          # BaseVCS 抽象类 + ChangedFile/ChangeType + glob 排除匹配
│   ├── git_vcs.py       # GitVCS：git diff --raw -z --find-renames / git show / git log
│   ├── svn_vcs.py       # SVNVCS：svn diff --summarize / svn cat (URL+@peg) / svn log
│   ├── folder_vcs.py    # FolderVCS：先快照两个端点，再用分块字节读取判断差异
│   ├── archive_vcs.py   # ArchiveVCS：解压 zip/tar 到临时目录，委托 FolderVCS 比对
│   └── multi_version_vcs.py # Git/SVN 多版本：历史身份追踪 + 文件级端点快照
├── diff_engine.py       # DiffEngine：遍历变更文件，difflib.HtmlDiff.make_table()
├── report_generator.py  # Jinja2 渲染 templates/report.html → 单文件 HTML
├── file_exporter.py     # 变更文件按目录结构导出到 old/ 和 new/ 目录
├── delivery_instructions.py # 生成上线删除/重命名操作说明
├── logger.py            # 简易日志，仅 warn/error 写文件（info 为空操作），512KB 轮转
├── templates/report.html # 单项目 HTML 报告模板（文件树 + 左右对比 + 变更清单弹窗）
└── templates/multi_report.html # 多项目总报告模板（项目分组文件树 + 左右对比 + 变更清单弹窗）
```

### 数据流

1. `main.py` 收集输入：项目路径、VCS 类型（Git/SVN/文件夹/压缩包/Git多版本/SVN多版本）、旧/新版本号或多版本列表、排除规则、输出目录
2. 根据 VCS 类型创建 `GitVCS` / `SVNVCS` / `FolderVCS` / `ArchiveVCS` / `GitMultiVersionVCS` / `SVNMultiVersionVCS` → `get_changed_files()` 获取变更文件列表
3. `DiffEngine.generate_diff()` 遍历文件，对文本文件用 `difflib.HtmlDiff.make_table()` 生成 side-by-side HTML；二进制文件跳过内容只设占位标记；内容完全一致且唯一匹配的删除+新增会合并为重命名
4. `ReportGenerator` 用 Jinja2 渲染模板 → 单文件 HTML
5. `FileExporter` 导出变更文件：内置 VCS 统一通过 `export_file_to_path()` 流式写入暂存目标，避免大文件形成整块内存副本；无法流式导出的旧扩展仍按旧合同完整读取并写出，不因未知或预计大小拒绝。读取或转换失败必须使本次导出失败，不能静默漏文件或用不可靠的空文本兜底。单项目的报告、`<项目名>_上线操作说明.txt` 和 old/new 目录在各自同盘暂存后一次成组提交；正式单项目源码 stage 必须放在批次根内部随机 wrapper，不得混进 `oldVersion/newVersion`，多项目内层导出则显式标记目标已是外层 stage。多项目全部成功后整体替换 old/new 根及另外两类产物，任一步失败都恢复原有输出。耗时生成前必须捕获所有正式目标的文件系统树状态，提交锁内恢复旧事务后再复核，目标变化时拒绝让慢任务覆盖新结果。事务根到 stage/target/backup 的所有现存路径组件都不得是链接或联接点。提交时写 `.comparetool_transaction_*.json` 恢复日志和 commit/rollback 决策标记；journal 和决策标记必须由独立的每用户私钥做 HMAC-SHA256 验签，并记录 stage/target/backup 身份。输出根可以是私钥路径的广义祖先，但正式输出目标不得指向或覆盖私钥；未通过验证或对象身份变化时只保留现场，禁止自动删除或替换。8 月 27 日前的 v1 无签名日志不能作为恢复或删除授权；严格识别后原样保留并告警，但不得永久阻断用户提交本次新生成的完整结果。stage 所有权标记记录持有 PID，活进程的暂存物不得被另一实例清理。启动扫描先只读识别真实候选，再只对候选目录加锁；只额外识别批次目录下一层严格命名的 `multi_run_*`，不得递归用户源码树或向普通输出子目录写锁文件。重命名文件导出时 oldVersion 使用 `old_path`，newVersion 使用 `file_path`。

事务补充合同：主流程必须把用户配置的输出目录作为可信根传给目标快照、stage 创建、提交锁和恢复逻辑，不能从计算出的批次事务根才开始检查祖先。报告、说明和源码 stage 必须直接创建在可信输出根，不能写入生成期间可能被替换成 junction 的 batch/multi_run 子目录；创建后且写入内容前还要复核路径组件。固定锁文件必须以排他创建/安全打开方式拒绝链接、联接点、硬链接和替换竞态。tree identity 必须流式绑定每个普通文件的内容摘要，不能只依赖大小和 mtime。恢复删除前先把路径原子移动到由签名 journal 的 token 和源路径确定性派生的同目录隔离名，再验证被移动对象身份；恢复状态机必须识别隔离待清理、旧目标已恢复等中间态并可幂等续做。不匹配时保留现场，禁止按旧路径直接删除。有效签名 journal 是中断恢复的独立授权，不能依赖提交后可能被 finally 清理的 stage marker；PID stage marker 只授权无日志孤儿 stage 清理。

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

**报告路径修正**：`main.py` 在生成 diff 后，对压缩包模式将 `diff_result.project_path` 覆写为压缩包文件名（而非临时目录），避免报告头部泄露临时路径。

**版本标识翻译**：`ArchiveVCS._to_folder_ver()` 将外部版本标识（zip 路径）映射为 `"old"`/`"new"` 后再委托给 `FolderVCS`。`FolderVCS._resolve_version_dir()` 只识别 `"old"`/`"new"` 和临时目录路径，不识别 zip 路径，必须经翻译层转换。内容、大小、摘要和流式导出均使用此翻译。

### 差异展示

`DiffEngine.__init__` 接收 `show_full_context` 参数（由 GUI 单选按钮控制）。`True` 时展示文件全部行（`context=False`），`False` 时仅展示差异上下文（`context=True, numlines=3`）。默认为全部内容。

用户的完整性原则是：对抗性审查不得让旧版本能生成的正常输入因“可能很慢/可能很大”而被拒绝、跳过明细或截断清单。正常文本默认不设单文件字节、行数、最长行、明细数、路径字节、文本字节、预计渲染行数和 HTML 字节上限，也不得使用新旧行数乘积、最长行字符乘积或组合工作量提前跳过。目录替换推断必须使用前缀树或等价的线性/近线性算法，不能枚举全部新旧路径笛卡尔积。读取返回 `None`、已知 raw 大小与实际读取长度不符、或源端点在任务期间变化时必须中止，因为继续会得到错误结果。Jinja2 必须流式写报告，不能先在内存中生成完整 HTML。纯格式变化和纯重命名仍须保留 F/R 语义。

报告补充合同：manifest 必须完整列出变更，不因预计体积截断；最终报告必须边渲染边流式写入同目录临时文件，成功后再原子替换，默认不设 HTML 体积上限。默认详情完整时 manifest 直接复用 `fileData`，不得把同一完整路径清单再次序列化；只有显式受限策略让二者不同时才生成独立清单。多项目展示路径在浏览器端拼接，不重复保存 `displayPath`。末尾换行格式净差异只允许在 CR/LF 规范化后两端恰好相差一个末尾 `\n` 时成立，不能用 `splitlines()` 把 FF、VT、NEL、U+2028/U+2029 当换行吞掉。

### 二进制文件处理

- `DiffEngine.BINARY_EXTS` 定义二进制扩展名集合（`.jar`, `.war`, `.class`, `.dll` 等）
- `DiffEngine._diff_file()` 对二进制文件提前返回，不读内容，`side_by_side_html` 设为占位提示
- 导出时所有文件（含二进制）统一走 `export_file_to_path()` 分块或子进程直写目标；Git/SVN 的换行转换也在同目录临时文件中分块完成，空文件正常成功，读取失败必须中止事务。
- Git 用固定 commit 的 `git show` 直写，SVN 用固定 URL/revision 的 `svn cat` 直写，Folder/Archive/多版本快照用分块复制；精确重命名匹配使用 blob OID 或分块 SHA-256，不得整文件载入内存。SVN 哈希临时文件必须使用 CompareTool 专用临时根，避免大型内容落到系统盘默认临时目录。
- Git 和 SVN 导出时自动检测 VCS 换行符策略，对文本文件将 LF 转为 CRLF，使导出文件与 Windows 工作副本字节一致（见换行符处理章节）

### 排除规则

内置默认排除规则定义在 `main.py` 的 `DEFAULT_EXCLUDE_RULES`。界面中的 glob 模式在 `base._match_glob()` 中转正则：
- 不含 `/` 的模式（如 `*.class`）自动匹配任意深度 → 添加 `**/` 前缀
- `**/` → 可选目录前缀 `(.*/)?`
- `**`（末尾）→ `.*`
- 单 `*` → `[^/]*`

默认模板偏通用，只排除 VCS 元数据、Java/Python/Node 常见构建产物、日志/临时目录、IDE 元数据和系统文件。项目配置、脚本、文档类文件（如 `README.md`、`gradlew`、`settings.gradle`、`gradle.properties`）不应默认排除，应由用户按项目自行添加。

### 换行符处理

Windows 上 `core.autocrlf=true`（Git）或 `svn:eol-style=native`（SVN）会导致仓库存储 LF、工作副本为 CRLF。`git show` / `svn cat` 返回仓库原始字节（LF），若直接导出会与工作副本文件字节级不一致。

- **Git**：先对固定 commit 批量执行 `git check-attr -z --source=<version> ... --stdin`，并在任务快照前后复核有效属性与 `core.autocrlf/core.eol`；`.gitattributes` 中 `-text` / `eol=lf` / `eol=crlf` 优先，未指定时再按固定配置处理。属性无法可靠读取或快照期间变化时中止生成。
- `text` / `text=auto` 文件在 `core.autocrlf` 和 `core.eol` 都未设置时，`core.eol` 的默认值仍是平台 `native`；Windows 导出必须转为 CRLF，不能把“未设置”误当成“不转换”。
- **SVN**：`SVNVCS._get_eol_style()` 对每个文件按所选 revision 执行 `svn propget svn:eol-style`，完整支持 `native` / `LF` / `CR` / `CRLF`。
- **文件夹**：直接从磁盘读取，不存在换行符差异
- **公共逻辑**：`BaseVCS._is_text_bytes()` 判断文本文件（不含 `\x00`），`BaseVCS._apply_crlf()` 用正则 `(?<!\r)\n` → `\r\n` 转换，避免重复转换已有的 CRLF

普通 Git/SVN 生成开始时必须把用户填写的可变版本标识固定为完整 commit OID/数字 revision，后续差异、内容、属性和导出均复用同一端点，报告仍显示用户原始输入。Git 文件若启用 `filter`、`working-tree-encoding`、`ident` 或旧 `crlf` 属性，应因无法可靠复现 checkout 字节而中止；SVN 文件启用 `svn:keywords`、变化目录启用 `svn:externals` 时同样中止。

### 编码

- **SVN 子进程输出**：`_run()` 读取原始字节，通过 `_decode_bytes()` 自动探测编码（UTF-8 → GBK → 回退）。影响 svn log、svn diff、svn info 等所有命令输出
- **SVN cat / 本地文件**：同样走 `_decode_bytes()`（UTF-8 → GBK）
- **Git 路径**：`_unescape_git_path()` 解码 `core.quotepath` 的八进制和 C 风格转义（包括 tab、换行、引号和反斜杠）。

### Shell 依赖

所有 VCS 操作通过 `subprocess` 调用 `git` / `svn` 命令行，`cwd=self.project_path`。

### 配置持久化

`compareTool_config.json` 保存项目路径、VCS 类型、输出路径、多项目任务列表、`recent_projects`、`project_exclude_rules` 和 `project_display_options`。配置必须先写同目录临时文件并 `os.replace()` 原子替换，避免进程中断破坏已有任务；序列化、写入或替换失败必须向用户报错，不得显示假成功。若现有配置本身无法解析，程序可用默认值启动，但必须告警并阻止覆盖原损坏文件。启动时会规范化 `multi_tasks` schema，损坏、缺字段、VCS 类型未知或同名的任务记录应记录警告并忽略，不能阻止窗口启动；旧多版本任务的 `new_version` 会迁移为“文件级首尾端点”。项目级配置按规范化绝对路径保存：Git/SVN/Git多版本/SVN多版本用项目目录，文件夹用新版本文件夹，压缩包用新版本压缩包完整文件路径。最近项目列表按 Git/SVN 家族分组，每组最多保留最近 10 个有效项目，旧配置没有 `recent_projects` 时应兼容为空并可由当前有效项目回填。新路径没有专属排除规则时，使用 `main.py` 内置默认模板；旧版全局 `exclude_rules` 不再作为默认模板来源。多项目任务添加/更新时保存排除规则和显示选项快照，后续项目默认配置变化不会偷偷影响已添加任务。输出批次名称不持久化，每次启动默认当天日期。
