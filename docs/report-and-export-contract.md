# 报告与导出完整性合同

本文件是差异展示、字节导出和输出事务的详细约束。修改 `diff_engine.py`、`stable_diff.py`、`report_generator.py`、报告模板、`file_exporter.py`、`path_safety.py` 或 VCS 端点写入流程前，须与 [AGENTS.md](../AGENTS.md) 一起阅读。多版本文件身份与首尾语义另见 [多版本文件端点设计](multi-version-file-endpoints.md)。

正常输入允许耗时生成，默认不因预计工作量拒绝、截断或跳过。无法确保路径、字节或事务完整性时必须失败，不能交付看似成功的错误结果。

## 导出事务

压缩包勾选「忽略最外层单一文件夹」时，两端都必须只有一个顶层目录；仅去掉一层。报告路径、排除规则、权限元数据、重命名旧/新路径、目录删除说明和导出均基于该内层根，不能只在界面隐藏前缀。报告和上线说明须记录两侧实际根名称。默认关闭；任一侧不满足条件则整次生成失败，不提交输出。

`FileExporter` 导出变更文件：内置 VCS 统一通过 `export_file_to_path()` 流式写入暂存目标，避免大文件形成整块内存副本；无法流式导出的旧扩展仍按旧合同完整读取并写出，不因未知或预计大小拒绝。读取或转换失败必须使本次导出失败，不能静默漏文件或用不可靠的空文本兜底。单项目的报告、`<项目名>_上线操作说明.txt` 和 old/new 目录在各自同盘暂存后一次成组提交；正式单项目源码 stage 必须放在用户配置的可信输出根下随机 wrapper，不得混进 `oldVersion/newVersion`，多项目内层导出则显式标记目标已是外层 stage。多项目每次使用独立 `multi_run_时间戳_随机标识` 目录，全部成功后成组提交本次 old/new 根及另外两类产物，任一步失败都恢复原有输出。耗时生成前必须捕获所有正式目标的文件系统树状态，提交锁内恢复旧事务后再复核，目标变化时拒绝让慢任务覆盖新结果。事务根到 stage/target/backup 的所有现存路径组件都不得是链接或联接点。提交时写 `.comparetool_transaction_*.json` 恢复日志和 commit/rollback 决策标记；journal 和决策标记必须由独立的每用户私钥做 HMAC-SHA256 验签，并记录 stage/target/backup 身份。输出根可以是私钥路径的广义祖先，但正式输出目标不得指向或覆盖私钥；未通过验证或对象身份变化时只保留现场，禁止自动删除或替换。8 月 27 日前的 v1 无签名日志不能作为恢复或删除授权；严格识别后原样保留并告警，但不得永久阻断用户提交本次新生成的完整结果。stage 所有权标记记录持有 PID，活进程的暂存物不得被另一实例清理。启动扫描先只读识别真实候选，再只对候选目录加锁；只额外识别批次目录下一层严格命名的 `multi_run_*`，不得递归用户源码树或向普通输出子目录写锁文件。重命名文件导出时 oldVersion 使用 `old_path`，newVersion 使用 `file_path`。

事务补充合同：主流程必须把用户配置的输出目录作为可信根传给目标快照、stage 创建、提交锁和恢复逻辑，不能从计算出的批次事务根才开始检查祖先。报告、说明和源码 stage 必须直接创建在可信输出根，不能写入生成期间可能被替换成 junction 的 batch/multi_run 子目录；创建后且写入内容前还要复核路径组件。固定锁文件必须以排他创建/安全打开方式拒绝链接、联接点、硬链接和替换竞态。tree identity 必须流式绑定每个普通文件的内容摘要，不能只依赖大小和 mtime。恢复删除前先把路径原子移动到由签名 journal 的 token 和源路径确定性派生的同目录隔离名，再验证被移动对象身份；恢复状态机必须识别隔离待清理、旧目标已恢复等中间态并可幂等续做。不匹配时保留现场，禁止按旧路径直接删除。有效签名 journal 是中断恢复的独立授权，不能依赖提交后可能被 finally 清理的 stage marker；PID stage marker 只授权无日志孤儿 stage 清理。

## 差异展示

`DiffEngine.__init__` 接收 `show_full_context` 参数（由 GUI 单选按钮控制）。`True` 时展示文件全部行（`context=False`），`False` 时仅展示差异上下文（`context=True, numlines=3`）。默认为全部内容。

`HtmlDiff` 递归失败时由 `stable_diff` 按行序配对替换段，保留全部内容和行内差异，继续遵循用户选择的上下文范围；不能通过提高递归上限或截断输入应付。未知编码用 `surrogateescape` 保留字节身份，先匹配再显示带底色的 `⟦0xXX⟧`，并以文件说明标注编码状态，不能用 replacement 字符合并真实差异。

用户的完整性原则是：对抗性审查不得让旧版本能生成的正常输入因“可能很慢/可能很大”而被拒绝、跳过明细或截断清单。正常文本默认不设单文件字节、行数、最长行、明细数、路径字节、文本字节、预计渲染行数和 HTML 字节上限，也不得使用新旧行数乘积、最长行字符乘积或组合工作量提前跳过。目录替换推断必须使用前缀树或等价的线性/近线性算法，不能枚举全部新旧路径笛卡尔积。读取返回 `None`、已知 raw 大小与实际读取长度不符、或源端点在任务期间变化时必须中止，因为继续会得到错误结果。Jinja2 必须流式写报告，不能先在内存中生成完整 HTML。纯格式变化和纯重命名仍须保留 F/R 语义。

报告补充合同：manifest 必须完整列出变更，不因预计体积截断；最终报告必须边渲染边流式写入同目录临时文件，成功后再原子替换，默认不设 HTML 体积上限。默认详情完整时 manifest 直接复用 `fileData`，不得把同一完整路径清单再次序列化；只有显式受限策略让二者不同时才生成独立清单。多项目展示路径在浏览器端拼接，不重复保存 `displayPath`。末尾换行格式净差异只允许在 CR/LF 规范化后两端恰好相差一个末尾 `\n` 时成立。差异拆行与统计也只识别 CR/LF，不能用 `splitlines()` 把 FF、VT、NEL、U+2028/U+2029 等分隔符吞掉；特殊分隔符参与真实字符匹配后才在 HTML 中显示为 `⟦U+XXXX⟧`，不改变导出字节，也不能与原文中的字面标记混淆。

## 二进制文件处理

- `DiffEngine.BINARY_EXTS` 定义二进制扩展名集合（`.jar`, `.war`, `.class`, `.dll` 等）
- `DiffEngine._diff_file()` 对二进制文件提前返回，不读内容，`side_by_side_html` 设为占位提示
- 导出时所有文件（含二进制）统一走 `export_file_to_path()` 分块或子进程直写目标；Git/SVN 的换行转换也在同目录临时文件中分块完成，空文件正常成功，读取失败必须中止事务。
- Git 用固定 commit 的 `git show` 直写，SVN 用固定 URL/revision 的 `svn cat` 直写，Folder/Archive/多版本快照用分块复制；精确重命名匹配使用 blob OID 或分块 SHA-256，不得整文件载入内存。SVN 哈希临时文件必须使用 CompareTool 专用临时根，避免大型内容落到系统盘默认临时目录。
- Git 和 SVN 导出时按固定端点的换行符策略转换，使导出文件符合所选版本应有的检出字节；并非所有文本都转换为 CRLF（见换行符处理章节）。

## 文件与目录名称

导出、多版本端点与文件夹快照写入前必须逐级校验真实目录项并排他创建文件，拒绝实际 NTFS 短别名碰撞，允许独立合法的 `~` 名称；路径型 writer 只可写入本次已占有的新目标。碰撞必须在调用覆盖写入接口前失败，不能让两个报告路径共享一个输出文件，也不能在失败清理时删除被碰撞的已有文件。

## 换行符处理

Windows 上 `core.autocrlf=true`（Git）或 `svn:eol-style=native`（SVN）会导致仓库存储 LF、工作副本为 CRLF。`git show` / `svn cat` 返回仓库原始字节（LF），若直接导出会与工作副本文件字节级不一致。

- **Git**：对固定 commit 批量查询属性，并复核 `core.autocrlf/core.eol`、filter 配置及系统/全局/info 属性文件摘要。`GitCheckoutSnapshot` 创建同对象格式的专用临时裸仓库，只读共享源对象，固定外部属性副本并隔离 Git 配置；使用 `GIT_ATTR_SOURCE` 与两个微小 blob 探针由原生 Git 判定属性的真实状态和换行模式。不能把 `cat-file --filters` 直接用于真实大文件；内容仍由 `git show` 流式落盘并分块转换，避免 Git 子进程为大文件创建完整转换副本。结束时清理普通/多版本的检出快照。
- 原生策略判定为自动 CRLF 时，按 Git `convert.c` 的完整字节统计处理：已有 CR/CRLF、NUL 或非文本控制字节密度过高时保留原文，不能只按“无 NUL”转换；显式强制文本则按原生结果处理含 NUL 文件。默认 EOL 仍是平台 native，`core.autocrlf` 优先级由原生 Git 决定。
- **SVN**：`SVNVCS._get_eol_style()` 对每个文件按所选 revision 执行 `svn propget svn:eol-style`，完整支持 `native` / `LF` / `CR` / `CRLF`。
- **文件夹**：直接从磁盘读取，不存在换行符差异
- **公共逻辑**：`BaseVCS._is_text_bytes()` 判断文本文件（不含 `\x00`），`BaseVCS._apply_crlf()` 用正则 `(?<!\r)\n` → `\r\n` 转换，避免重复转换已有的 CRLF

普通 Git/SVN 生成开始时必须把用户填写的可变版本标识固定为完整 commit OID/数字 revision，后续差异、内容、属性和导出均复用同一端点，报告仍显示用户原始输入。Git filter 名保留大小写，按驱动成组固定并复核 `smudge`、`process` 和 `required` 配置；没有检出程序且未要求强制转换的 filter（包括仅配置 clean）允许按普通文件导出。实际启用检出转换或要求强制转换的 filter，以及启用的 `working-tree-encoding`、`ident` 或旧 `crlf` 属性，仍因无法可靠复现 checkout 字节而中止。`check-attr` 输出中的 `set`/`unset`/`unspecified` 可能是同名字面 filter 驱动，必须由隔离 Git 区分；只复制“必需但无程序”的拒绝策略，不复制或执行 smudge/process，真正的 `-filter`/`!filter` 等状态仍可正常导出。普通 SVN 和 SVN 多版本均允许空值或仅含 ASCII 空白的 `svn:keywords`；`svn:externals` 的空白和整行注释不视为有效定义，其它定义及非空 keywords 仍须中止。

## 验证入口

压缩包根目录回归见 [test_archive_comparison_root.py](../tests/test_archive_comparison_root.py)：默认完整路径、只进入一层、ZIP/TAR、相对排除规则、权限元数据、重命名、报告与导出一致、异常结构中止清理，以及 GUI 和多项目任务选项保存。

新增完整性回归位于 [test_complete_export_review_fixes.py](../tests/test_complete_export_review_fixes.py)：真实 Git 普通/多版本的 NTFS 别名碰撞及旧输出保留，合法 `~` 名称，520 行相似文本递归复现，随机差异表内容还原、上下文，以及未知编码字节与字面标记不混淆。

在项目根用 `.\.venv\Scripts\python.exe -m unittest discover -s tests -q` 运行完整回归。正式打包后还须启动新 `dist/CompareTool.exe` 做窗口响应和 Python/Tk 启动冒烟；源码测试和打包成功不能代替真实项目验收。
