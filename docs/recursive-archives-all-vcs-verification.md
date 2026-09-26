# 全模式递归压缩包报告：实现与验收

源码基线：`8b7be5f`。执行规格：`recursive-archives-all-vcs-plan.md`。
本轮采用方案第 3 节推荐的“分析已导出但尚未提交的暂存文件”，未采用第二套仓库缓存。
范围为源码、测试和说明书；不更新版本号、不打包或替换 EXE、不提交或推送。
原始记录：`.tmp/all_vcs_eexjr2lu/`。最终测试数量和浏览器结果见本文末尾实测记录。

## 实际流程

单项目递归关闭时维持原有顺序。开启时先取得原有主 DiffResult，完成
`prepare_export()`，根据其实际返回的 target/stage 对确定新旧源码暂存根。
在递归前捕获含内容摘要的暂存身份，然后分析这些已完成检出转换的交付字节。
HTML 和上线说明仍在独立 stage 中生成，四类产物最终共同提交。

多项目先完成全部项目的主比较和导出，再一次绑定两个外层 stage 根；之后仅对
启用的任务递归，各自使用项目子目录和选项，共享一份 ArchiveReportBudget。
不能在还有项目写入时提前绑定外层目录，否则会把自身写入误判为并发篡改。

提交锁内，在建立当前事务的备份/安装状态之前，复核传入的暂存基线。
已核验的 identity 直接进入现有 journal，不在接受新的内容之后重新定义基线。
报告与说明沿用原来的生成/事务身份规则；HMAC、回滚、恢复和所有权协议不变。
递归关闭或主清单没有受支持的包候选时，不增加这一整树绑定扫描。

## 代码位置

- `archive_endpoints.py`：显式 old/new 侧别、安全路径和只借用的暂存端点。
- `archive_report.py`：可注入端点来源，旧 folder/archive 直接调用仍兼容；
  R 的旧路径、A/D 的不存在侧、项目/路径/嵌套错误上下文、严格 LFS 指针诊断。
- `main.py`：六模式配置链路、单/多项目阶段顺序和共享预算。
- `file_exporter.py`：`capture_stage_states()` 与可选 `expected_stage_states`。

未改动 Git/SVN 版本固定、项目范围、检出策略、多版本历史规划、路径安全或解压器。
`stable_diff.py` 及递归 include 模板保持 `8b7be5f` 的修复，不回退宏缓冲。
不向主 files 添加包内成员，不生成包内成员的服务器删除指令，不重新打包。
新增模块不拥有借用的 stage；stage 的销毁仍由原 exporter/finally 及恢复协议处理。
LFS 诊断只读最多 1024 字节；合法指针明确失败，不联网下载、不执行 filter。
相同字节包优先跳过，含同样无效内容的包也不宣称完成格式校验。

## 验收矩阵落点

下面区分新增端到端和沿用既有历史回归；不将 mock 视作真实仓库验收。

| 方案范围 | 本轮证据 |
|---|---|
| C01-C06 配置与界面 | `test_archive_report_configuration.py`：六模式启用、旧配置/字符串布尔值、实际 Tk 最近项目切换和重启、真实 Git GUI 线程参数快照；旧 archive GUI 回归继续运行 |
| H01-H03 固定历史身份 | `test_archive_report_all_vcs.py`：工作包故意 WRONG；固定后移动 Git tag；固定后 SVN switch 并推进 HEAD；报告/交付仍为指定包 |
| H04-H10 路径、元数据及范围 | 真实 Git/SVN A/D/R、属性/mode、跨后缀改名；Git 子项目普通/多版本与裸仓库；同名多项目及不同版本不串用；原边界/转换回归 |
| M01-M04、M08 多版本端点 | Git/SVN 各自 A/B/C 包不同首尾、未选中中间内容、最早选中前改动、root 新增及净零、分号/换行选择标签 |
| M05-M07 历史移动约束 | 新增 Git 未选中改名和 SVN 根/嵌套目录/成员改名的包端到端；原 merge/first-parent/shallow/copy/rebuild 历史套件 |
| B01 六模式 off/on | 各模式真实生成主列表、summary、manifest、上线说明及导出路径/流式 SHA-256 相等；混合报告重复验证 |
| B02-B08 包内语义 | 既有递归用例覆盖 TAR/WAR/JAR、压缩 TAR、空包、相同子包、元数据、过滤和单根；新增 8/9 层边界、缺失侧拒绝 |
| B09-B10 检出字节与指针 | 显式 EOL 使前导文本改变但 ZIP 仍有效的包必须成功；转换损坏包、LFS 指针、有效 smudge filter 明确失败，标记文件不产生 |
| F01-F06 原输出完整性 | 真实 Git/SVN 内容读取中途写失败；最后一个混合项目损坏；跨项目共享预算；报告/说明失败；安装中途失败，四类旧输出保留 |
| F07-F08 暂存绑定 | 分析后同大小同时间戳原地修改与替换都拒绝提交；真实 junction 在打开成员前拒绝；正式目标并发变化沿用原回归 |
| F09-F10 恢复和清理 | 实际 Windows deny-delete 占用解除后重试、旧恢复套件、无候选不额外扫描、借用树不修改，任务结束无残留 stage/journal |

新增文件：`test_archive_report_all_vcs.py`、`test_archive_report_staging.py`、
`test_archive_report_configuration.py`；辅助仓库与流式摘要在 `archive_workflow_fixtures.py`。
旧集成用例已移到真实导出后的分析阶段，不再要求 `_prepare_task_result()` 提前递归。

## 实测记录（2026-09-26）

独立源码副本完整回归：492 项，491 通过，1 跳过，0 失败、0 错误。
跳过的是原有 Windows 文件符号链接创建用例（WinError 1314）；不是四种新增模式。
本轮真实本地 Git、SVN、svnadmin 验收及 junction、文件占用用例均实际执行。
原始记录为 `full-regression/tests.json`、`tests.log`，运行约 533.75 秒。
部分旧 Tk 测试清理后产生 after 回调警告；本轮真实 GUI 用真实 mainloop 验收，
完成时退出并取消测试窗口的剩余回调，所有结果断言通过。

额外历史探针 `history-probes.json` 验证普通 SVN 项目根移动的包端点、
Git/SVN 改名且修改以及跨排除边界、向测试实例注入不可用 OID 后真实 Git 命令
失败仍保留旧产物。未删除仓库对象。普通 SVN 修改后移动可能保留为 D+A，
这是上游既有识别行为；本轮不凭推测强行生成 R，off/on 主结果保持一致。

单 Git 全文、单 SVN 上下文、六模式混合报告都在独立 Edge headless 实际执行。
检查 TAR/WAR/JAR/文本逐层展开、旧新正文、原始行号范围、局部导航、切回普通文件、
成员名转义、页面脚本/console.error/未处理 Promise 错误。结果见 browser-results.json。
源码真实 Tk Git 后台线程任务也单独验证，不以后台方法 mock 代替 GUI 全链路。

### 三次交替 off/on 基准

`tools/benchmark_archive_all_vcs.py` 使用本地小仓库/目录，六模式每种开关各三次。
工作线程总时间包含导出、递归绑定与分析、报告/说明、事务提交和清理；
不含夹具创建、应用启动或浏览器，也不对应用户大型业务数据。同期有完整回归。

| 模式 | off 中位秒 | on 中位秒 |
|---|---:|---:|
| Git | 3.082 | 3.152 |
| SVN | 0.894 | 1.022 |
| Git多版本 | 3.239 | 3.408 |
| SVN多版本 | 1.295 | 1.305 |
| 文件夹 | 0.351 | 0.397 |
| 压缩包 | 0.324 | 0.360 |

36 次运行中递归阶段的内容子进程新增次数均为 0。另有用例在递归调用期间
禁止 Git/SVN writer 和 subprocess.run，并用不可访问的 VCS 对象验证只读取
注入的 stage；没有为了内部报告第二次下载/获取包。

基准的内容计数是进程启动数，Git batch 一个进程可服务多个对象，不冒充逻辑请求数。
独立的一次 tracemalloc 记录递归阶段额外 Python 分配峰值约 1.23 MB；不包含
先前主结果对象，不是整个进程 RSS，也不是大型包的内存上限。内存跟踪没有混入
上述三次耗时中位数。完整分阶段数据、所有运行与计数在 benchmark.json / benchmark.log。

## 文档与发布状态

AGENTS、README、完整性合同、包内规则和说明书维护源已同步。
说明书仍为 12 页，书签/目录、嵌入字体、缺失字形及文本边界检查通过；
全部页面渲染复核，docs 与 dist 的 PDF 副本字节一致。
本轮没有新 EXE：app_version.py、正式 CompareTool.exe、构建元数据和用户配置
必须与开始时摘要一致。没有自动 Git 提交、推送；原方案文件保留原样。
当前源码可用 `.venv\\Scripts\\python.exe main.py` 验收；正式构建留待另行授权。
