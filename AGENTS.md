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

模块职责见 [README 架构与版本标识](README.md#架构与版本标识)，实现前同时遵守下列数据流和对应合同。

### 数据流

六模式输入经对应 VCS 固定端点，交给 DiffEngine 生成差异和 ReportGenerator 渲染；模块流程见 [README](README.md#架构与版本标识)。

`FileExporter` 通过 `export_file_to_path()` 流式写入新旧变更文件，确认覆盖后先删除本次项目的报告、上线说明和 oldVersion/newVersion 项目目录，再重新生成；失败不恢复旧结果。输出批次锁覆盖删除到任务结束，新内容完整暂存后发布，不再使用旧输出备份、事务日志、HMAC 或回滚。历史恢复记录不参与新任务且原样保留。暂存物直接创建在可信输出根，保留路径边界、实际 Windows 名称碰撞及本次交付字节检查。实现或修改报告、端点写入、导出和恢复流程前，必须阅读 [报告与导出完整性合同](docs/report-and-export-contract.md)。

项目名只在能从有效项目目录、新版本文件夹或新版本压缩包推断出真实名称时自动填充。推断不到且用户未手工填写时，生成报告或添加多项目任务应直接提示失败，不使用 `project` 之类的假兜底名称。Git/SVN/Git多版本/SVN多版本模式下，项目名输入框是可编辑下拉框，会按 Git/SVN 家族记忆最近 10 个有效项目；选择最近项目时必须同步回填项目目录和项目名，并触发项目路径变化逻辑清空版本选择和版本列表，避免跨项目复用版本号；多版本只读的“生成结果”仍须显示“文件级首尾端点”。

### 多项目总报告

`main.py` 维护 `multi_tasks` 任务列表，每个任务保存项目名、VCS 类型、路径/版本、排除规则、是否使用项目名、差异展示方式等快照。输出批次名称是当前这次输出的全局字段，启动时默认 `yyyyMMdd`，不按项目记忆、不保存到配置、不写入多项目任务快照。生成多项目总报告时，逐个任务创建对应 VCS、按任务自己的差异展示方式运行 `DiffEngine`，处理完各项目后统一调用 `ReportGenerator.generate_multi()` 渲染 `templates/multi_report.html`，并用 `FileExporter` 导出到：

```
oldVersion/项目名/...
newVersion/项目名/...
```

若填写了输出批次名称，批次根目录会变成 `输出目录/输出批次名称/`。单项目报告和导出仍生成在批次根；每次多项目生成则必须在批次根下创建独立的 `multi_run_yyyyMMdd_HHmmss_SSS_<8位随机十六进制>/` 运行目录，报告、`上线操作说明.txt`、`oldVersion` 和 `newVersion` 全部位于该运行目录内。随机后缀用于消除同一毫秒并发碰撞；历史运行目录原样保留。这样历史多项目报告不会引用后一次运行的说明或源码包，也不会与同批次单项目导出互相覆盖。生成前提示的“实际输出目录”必须是本次真正写入的批次根或 multi run 目录。

多项目任务允许混用 Git/SVN/文件夹/压缩包/Git多版本/SVN多版本。独立项目失败时保留未完成记录，其他项目继续生成；报告、说明、完成提示及耗时状态必须明确标注部分交付。总报告文件名格式为 `multi_compare_report_yyyyMMdd_HHmmss_SSS.html`，并与该次运行的说明和源码包共同保存在独立 multi run 目录。任务列表保存到 `compareTool_config.json`。多项目项目名按 Windows 大小写不敏感规则判重，避免 `Demo` / `demo` 写入同一目录。多项目变更清单是纯文本页面，按新增/修改/格式变化/删除/重命名汇总；每条路径是否带项目名由该任务自己的 `show_project_root` 决定。生成总报告前会检测最终展示路径冲突，若同一展示路径来自多个项目，则自动为相关任务补充项目名前缀并在报告注明，不修改任务保存的偏好。`上线操作说明.txt` 中的路径始终带项目名。

### VCS 类型与版本标识

六模式输入及版本标识见 [README 架构与版本标识](README.md#架构与版本标识)。Git/SVN 多版本必须使用下文的文件级首尾端点语义。

### 版本列表交互

Git/SVN/Git多版本/SVN多版本的版本列表只搜索当前已经展示的列表内容，不额外查询仓库。普通 Git/SVN 模式仍由 `GitVCS.get_versions()` / `SVNVCS.get_versions()` 获取 tags/分支/最近 100 条日志或最近 100 条 revision；多版本模式仍由 `get_recent_versions()` 获取当前项目最近 100 条主线提交或相关 revision。

版本列表工具条包含搜索框、清空按钮、填入按钮，以及「隐藏版本列表 / 显示版本列表」切换按钮。隐藏只收起 `Listbox`，不清空 `_version_items`、搜索词或多版本选择状态；重新获取版本列表、切换项目路径或切换 VCS 类型时应重置版本列表状态并自动展开。Git多版本/SVN多版本多选要通过 `_selected_multi_versions` 保留跨搜索过滤、隐藏/显示后的选择，填入时按原始版本列表顺序输出。

### 重命名处理

`ChangeType.RENAMED` 中 `file_path` 表示新路径，`old_path` 表示旧路径。`GitVCS` 使用 `git diff --raw -z --find-renames` 获取 Git 明确识别的重命名和类型变化。普通 SVN、文件夹和压缩包可由 `DiffEngine._merge_exact_renames()` 把内容字节完全一致且唯一匹配的 `DELETED + ADDED` 合并成 `RENAMED`。Git多版本/SVN多版本由端点规划器沿历史追踪文件身份，禁止再对最终删除/新增做内容二次配对。重命名只发生编码/BOM/换行变化时要显示明确说明；排除规则只命中新旧一侧时必须转换为删除或新增。普通 Git 比对遇到 `T` 类型变化拒绝该路径，桌面入口记录未完成后继续其他文件；Git 多版本历史中的 `T` 只用于延续同路径身份，若最终选中端点不是普通文件，严格 API 拒绝，桌面入口将该文件列为未完成并继续其他文件。报告模板必须把 `R` 纳入汇总卡片、文件树标签、过滤器和纯文本变更清单。

同 commit/revision 的多个 rename 必须先统一绑定旧身份，再更新目标路径，不能把两个文件的 `a→b、b→c` 合并成一个文件。部分交付同时检查同路径复用和目录替换依赖，失败 rename 两端关联的写入/删除必须整组退出，独立文件继续。回归见 `tests/test_review_path_dependencies.py`。

### Git/SVN 多版本文件端点

Git 普通/多版本均以用户选择的目录作为项目范围，允许仓库根、子目录和裸仓库。Git 命令在仓库根执行；报告、排除规则、身份规划和导出使用项目相对路径，读取对象和属性时映射回仓库相对路径。跨项目边界的 rename 在历史竞争前降级为本项目一侧的新增或删除，不得混入兄弟项目，也不得重复添加所选子目录前缀。裸仓库以仓库根作为范围，输出仍须位于仓库元数据之外。

Git多版本/SVN多版本使用“文件级首尾端点”语义：选中版本只决定候选文件集合及每个文件的首次/末次选中变更；old 取该文件首次选中变更之前的真实状态，new 取末次选中变更之后的真实状态，只比较最终净结果。不同文件允许来自不同 commit/revision；报告和 oldVersion/newVersion 使用同一端点；newVersion 导出完整文件，但不是某个单一版本的完整项目快照。

- `GitMultiVersionVCS` 只接受当前分支第一父历史的选中提交，合并提交相对第一父提交计算；浅克隆缺少父对象时失败。历史按正常阈值追踪重命名；选中历史不连续时，只有紧凑候选组中的不同逻辑实体实际跨越选中端点，才运行低阈值第二遍 diff 或隔离 source/target blob 的 Git 原生 rename score，每个命中组均按真实路径首尾端点降级为 D/A/M，不为无关历史提前展开或评分 `D/R × A/R/M` 笛卡尔积。跨提交待定删除也只保存活动区间和目标事件，最终按时序过滤后评分。候选身份跨选中端点且不唯一时按冲突连通组重新规划真实路径端点，不猜测 rename。
- `SVNMultiVersionVCS` 解析当前项目 URL 的 `svn log --xml -v`，按 revision 映射项目根/祖先移动前缀，用 `copyfrom-path` 和删除覆盖关系追踪文件身份；同 revision 根移动加子文件改名、嵌套目录移动、子文件移出目录或覆盖已有目标、延迟 copyfrom、移动后删除源祖先都必须保持身份。目录移动同 revision 又从继承后的原后缀复制新文件时，只要原后缀仍存在，就必须视为普通 copy；同一个源分叉到多个目标且自然后缀消失时，安全降级为删除源和新增各目标，不猜测唯一 rename。
- 两种模式均不执行 cherry-pick 或 SVN merge；业务排除规则在历史身份及分叉规划完成后、最终端点正文读取前逐侧应用；中间经过排除目录不切断身份，排除某个分叉目标不改变真实身份歧义。最终端点双侧排除跳过，旧侧排除为新增，新侧排除为删除；规则冻结于本次快照，更改规则须新建任务。导出快照与仓库原始字节快照分离，并直接流式写入磁盘端点后分块比较，不得同时在内存保留四份内容。Git 重命名候选和评分默认不设性能上限，评分 blob 必须流式落盘；候选身份无法唯一确认时按冲突组真实路径降级，不能猜测。Git 多版本历史中的类型变化只延续同路径身份；最终选中端点出现非普通 mode、`svn:special` 或其它非普通文件时，必须在净零过滤前拒绝该文件；桌面入口记录未完成后继续其他独立文件，严格 API 中止。成功或失败后必须清理临时目录。
- Git 选版覆盖最早到最晚的完整第一父历史时，仍逐提交规划各文件真实首尾端点并保留原生 rename、明确 D/A，跳过不同实体间额外的候选配对和歧义拒绝；中间有任一未选提交则保留原有全部身份检查。不能用全局首尾读取替代文件自己的属性、模式和字节端点。回归见 `tests/test_multi_version_file_endpoints.py`。
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

修改解压、比较根或临时目录生命周期前，必须阅读 [归档输入与临时目录](docs/report-and-export-contract.md#归档输入与临时目录) 及其链接的 ZIP/TAR 专项合同。`ArchiveVCS` 委托 `FolderVCS` 比较；排除规则、外部版本标签、权限元数据和比较根必须同步映射，报告不能泄露临时目录。

格式由稳定源句柄的实际内容识别，后缀仅用于筛选提示；选包默认显示所有文件。普通 GZIP（含 `.dat.gz`）按单文件流校验与比较；外层假压缩后缀文本拒绝作为包，包内此类文本按文本比较，真实损坏包仍注明未展开。支持及命名边界见上述合同。

保留源句柄和稳定文件 ID，逐项验证路径、类型、长度、CRC、重复成员及真实 Windows 名称碰撞；不使用无过滤的 `extractall()`，不设置默认性能型规模上限。固定排除只减少落盘，正文校验和全量计数不减少，公共动态 setter 不得读出占位内容。忽略单一外层目录仅剥一层，两端成对确定或回退；严格 API 仍拒绝不适用结构。临时目录仅回收七天前、有效所有权标记且原进程已退出的专用目录，无效标记原样保留。

### 差异展示与二进制导出

默认展示全部内容，可选前后各 3 行上下文。正常输入默认不设性能型规模上限；`HtmlDiff` 递归失败时保留完整逐行差异，未知字节无损标记，F/R 语义与原始导出字节必须保留。二进制仅跳过内容展示，仍完整流式导出。具体编码、拆行、统计、manifest、写入与覆盖约束以 [报告与导出完整性合同](docs/report-and-export-contract.md) 为准。

### 排除规则

内置默认排除规则定义在 `main.py` 的 `DEFAULT_EXCLUDE_RULES`。界面中的 glob 模式在 `base._match_glob()` 中转正则：
- 不含 `/` 的模式（如 `*.class`）自动匹配任意深度 → 添加 `**/` 前缀
- `**/` → 可选目录前缀 `(.*/)?`
- `**`（末尾）→ `.*`
- 单 `*` → `[^/]*`

排除规则按模式生效：文件夹和归档可提前筛选，Git/SVN 多版本先追踪完整项目历史与分叉，再过滤最终端点，不能只过滤最终报告。整树排除证明同时覆盖该目录和所有后代，仍须由同一条规则成立；文件夹已剪枝目录只保留名称拓扑，不打开该目录或其后代的身份/正文。`snapshot=False` 的固定目录同样先排除再比较。SVN 多版本的 log/list 保留被排除路径的必要身份元数据，分叉只在跨选中端点时按真实路径重新规划，支持连续未选分叉；最终排除端点不生成正文快照。文件/目录替换须先区分真实新旧侧，单侧排除不能漏掉另一侧；整目录替换会连带删除排除旧内容时相关依赖组列未完成并退出交付。报告、manifest、导出和上线说明只消费过滤后的交付集合；递归在每层包根应用规则，原包字节不变。输入身份、输出根/锁/发布边界及归档结构、路径穿越、链接、CRC、重复冲突校验不属于业务文件筛选。回归见 `tests/test_exclusion_path_flow.py`。

默认模板偏通用，只排除 VCS 元数据、Java/Python/Node 常见构建产物、日志/临时目录、IDE 元数据和系统文件。项目配置、脚本、文档类文件（如 `README.md`、`gradlew`、`settings.gradle`、`gradle.properties`）不应默认排除，应由用户按项目自行添加。

### 换行符处理

导出字节必须符合固定端点的 Git 属性及配置或 SVN `svn:eol-style`；文件夹保留原字节。Git 属性由隔离的原生探针判定，不执行外部检出程序；无法可靠复现的有效转换必须拒绝该文件；桌面入口记录未完成并继续其他文件，严格 API 中止。普通和多版本模式复用同一规则，完整支持与拒绝边界见 [报告与导出完整性合同](docs/report-and-export-contract.md#换行符处理)。

### 编码

子进程、文件及 Git 路径解码约定见 [README 架构与版本标识](README.md#架构与版本标识)；修改解码不得丢失路径或原始导出字节。

### Shell 依赖

所有 VCS 操作通过 `subprocess` 调用 `git` / `svn` 命令行。SVN 使用项目目录；Git 仓库命令使用固定仓库根，并显式映射项目相对路径。隔离检出探针只读取固定端点属性，不执行用户配置中的外部转换程序。

### 配置持久化

`compareTool_config.json` 保存项目路径、VCS 类型、输出路径、多项目任务列表、`recent_projects`、`project_exclude_rules` 和 `project_display_options`。配置必须先写同目录临时文件并 `os.replace()` 原子替换，避免进程中断破坏已有任务；序列化、写入或替换失败必须向用户报错，不得显示假成功。若现有配置本身无法解析，程序可用默认值启动，但必须告警并阻止覆盖原损坏文件。启动时会规范化 `multi_tasks` schema，损坏、缺字段、VCS 类型未知或同名的任务记录应记录警告并忽略，不能阻止窗口启动；旧多版本任务的 `new_version` 会迁移为“文件级首尾端点”。项目级配置按规范化绝对路径保存：Git/SVN/Git多版本/SVN多版本用项目目录，文件夹用新版本文件夹，压缩包用新版本压缩包完整文件路径。最近项目列表按 Git/SVN 家族分组，每组最多保留最近 10 个有效项目，旧配置没有 `recent_projects` 时应兼容为空并可由当前有效项目回填。新路径没有专属排除规则时，使用 `main.py` 内置默认模板；旧版全局 `exclude_rules` 不再作为默认模板来源。多项目任务添加/更新时保存排除规则和显示选项快照，后续项目默认配置变化不会偷偷影响已添加任务。输出批次名称不持久化，每次启动默认当天日期。

文件夹、压缩包的新旧路径按模式保存在 `last_comparison_paths`，离开模式时保留内存状态，生成、保存任务或正常退出时随配置原子保存；切换回模式及启动时恢复。旧配置缺字段兼容为空，损坏记录忽略，暂不可用的路径仍保留输入。Git/SVN（含多版本）的版本清空和项目配置切换规则不变。

## 性能实现与验证边界

- 大替换段可提前选 stable；64 行只是算法切换点，小修改保留 HtmlDiff。先剥离公共边界再匹配，保留完整正文、行号、上下文、格式语义与独立统计。测试核对用户可见内容，不绑定某个渲染器。
- 原始 Git batch 与微小 filters 通道用途分开。raw 按 1 MiB 分块并检查 blob 类型、长度、结束符；协议/写入错误关闭通道，缺失不能当空文件；显式超时和无法表示的路径保留单次调用。cleanup 关闭进程，实际检出转换不能跳过。详见 [完整性合同](docs/report-and-export-contract.md)。
- 基准至少三次，核对报告正文和导出字节，注明计时是否含旧输出删除和新结果发布；入口 `tools/benchmark_performance.py`。测试 `_tmp_root` 只能是独占目录，不能用 cwd；完整回归在独立源码副本运行，基准不清理固定已有目录。历史结果见 [第一批记录](docs/performance-first-batch.md)。

## Windows 文件夹扫描

- 链接判断复用 lstat 的 reparse tag，旧环境保留回退。安全打开用 `FILE_FLAG_OPEN_REPARSE_POINT`，检查句柄 tag/FileId；不能恢复先查路径再普通打开的竞态。
- 初始身份可复用同一次安全打开；内容读取前后身份和源树最终复核仍保留。叶文件可复用随后安全打开的检查，遍历目录前仍拒绝 junction/symlink。
- 只缓存比较根 realpath，键保留绝对路径大小写，不用 normcase 合并大小写敏感目录；目标 realpath 每次重新解析。测试见 `test_folder_scan_optimization.py`、`test_folder_scan_edge_cases.py`，基准及边界见 [扫描记录](docs/performance-folder-scan.md)。
- Windows ctypes 结构和 API 签名在模块内复用，每次查询仍分配独立结果并读取当前句柄，不缓存 FileId、大小、时间或 reparse tag。相对路径的纯字符串校验使用有界不可变缓存，公共接口返回独立列表；不得缓存目标 realpath 或用目录枚举元数据替代读取前后、最终源树复核。后续验证见 [扫描优化续篇](docs/performance-folder-followup.md)。

## 构建身份与任务观测

- 正式版本号由 `tools/build_release.py` 按本地构建日期生成 `yyyy.MM.dd.N`，读取目标目录及旧构建归档的元数据取当天最大序号加一，跨日从 1 开始；运行时标题和版本信息均读取嵌入版本。源码显示开发版，缺失或无效元数据为 unknown，不再用源码中的固定日期充当正式版本。
- 使用 `build.bat` / `tools/build_release.py` 嵌入时间、父提交、dirty 与源码指纹，源码构建号显示 source、缺元数据显示 unknown，不猜构建号。正式入口仅 `dist/CompareTool.exe`，保留构建元数据并归档旧构建；发布仍须真实窗口冒烟。
- 观测独立于业务成败；ContextVar 隔离任务，主线程每 200ms 读最新槽，不逐文件排 Tk 回调/日志，不伪造百分比，清理和任务记录后才通知完成。阶段包含时间不能相加，使用 exclusive_seconds。
- 耗时日志不写正文、不写入输入树；成功、失败和中断日志合计保留最近 50 组，进行中任务除外；每组过程最多 512 KiB，结束事件及摘要各最多 64 KiB。普通日志和一份备份各最多 512 KiB，单条最多 64 KiB。任务观测机制见 [正式版记录](docs/release-polish.md)，最新发布验证从 [README](README.md#正式入口版本与任务进度) 进入；界面变化同步说明书。

## 包内递归与暂存端点

- 六模式及混合任务均支持，默认关闭并保存任务快照；只挂 `archive_details`，不改变主清单/统计/manifest/导出/上线指令，不改包、不重压缩。相同包不继续展开，也不宣称验证了其格式；CLASS 不反编译。
- 递归层数、累计成员数和累计展开字节默认不限，不能因预计规模或耗时阻断完整报告。全报告保留共享计数（多项目不重置），声明量与实际读取量含丢弃的排除正文；只有显式注入资源策略时才按其上限拒绝。包内解析失败逐包注明未展开并保留原包；实际交付文件不能验证时列为未完成，不能静默漏项。
- 包内分析和模板层级遍历使用显式栈，不依赖 Python 调用栈或提高递归上限；模板按层级事件流式写出并保留惰性 DOM，父层资源在子层完成后释放，失败和中断也须清理。名称转义，导航及焦点局部隔离，键盘激活按钮保留焦点。单/多项目均核对完整成员与最大输出块；详见 [包内报告合同](docs/nested-archive-report.md)。
- GUI 先完成 exporter，再用显式 old/new 侧别借用实际 stage；多项目全部导出后绑定外层根，不重新读仓库/工作副本。capture_stage_states 基线在本次发布前后核对；无候选不额外整树哈希，不再对旧输出做摘要或生成 journal。借用者不清理 stage。
- 只分析真实交付字节，不可解析时保留原包并注明未展开，不回退 raw、下载 LFS 或执行 filter。保留并发互斥和本次暂存所有权；开关前后核对交付字节与说明一致。完整流程见 [暂存合同](docs/report-and-export-contract.md) 和 [六模式验收](docs/recursive-archives-all-vcs-verification.md)。源码验收不等于发布 EXE，打包另行授权。

## 性能资源复用约束

修改缓存、批量读取、归档计划、HTML 片段或发布流程前，必须阅读 [性能优化中的资源复用](docs/report-and-export-contract.md#性能优化中的资源复用)、[批量读取与完整渲染](docs/report-and-export-contract.md#批量读取与完整渲染补充2026-09-24) 和 [覆盖与重新生成](docs/report-and-export-contract.md#覆盖与重新生成)。

缓存仅复用固定身份或纯函数结果，不能改变端点字节、失败语义或输入规模；raw/export 保持独立普通文件，实际检出转换不能省略。报告公共 API 继续默认保留正文和 HTML 字符串，工作线程显式启用片段存储；按行/块消费，任务结束释放，不能接管交付 stage。输出发布的有限重试仍复核路径、身份和原基线，不覆盖外部新目标，不恢复旧结果。

归档排除、重复成员、TAR 计划与计数遵守同一完整性合同；具体回归入口见 [C1 验证](docs/performance-c1-verification.md)、[覆盖重建验证](docs/output-regeneration.md)、[重复成员](docs/archive-duplicate-members.md) 和 [TAR 名称](docs/tar-directory-compatibility.md)。
