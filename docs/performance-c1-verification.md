# C1 HTML 片段存储与逐行渲染：接续实现记录

## 基线与范围

本轮在已经完成 A、B、C2 的未提交源码上继续，不回退到 Git HEAD。
HEAD 为 `13578d0656c904287aad6c7ef69d06d1e6e37d09`；
实际接续前应用源码指纹为 `f923cd344e07d47430a52cc0f6f5aed60262fa07c07a9386b3c9bd63ff677944`。
证据目录：`.tmp/perf_c1_hv3uhvgx/`，其中 baseline 是接续前快照，
candidate_a 是 C1a，candidate_b 是加入 C1b 后运行回归的快照。
正式 EXE、版本、用户配置、原方案、业务输入均不属于本次修改范围。

## 实现

### C1a：一个任务共用一份私有片段文件

`html_details.py` 的 HtmlDetailStore 懒创建一份二进制 TemporaryFile，
位于已有 temp_storage 管理的独占目录，并避开本次输入路径。
不为每个文件或包成员建立独立临时文件，不增加全局缓存或数据库。
片段引用记录所属 store、UTF-8 字节偏移、字节长度和摘要。
只有全部写入、flush 及长度检查成功后才发布引用；写入失败使 store 失效。
读取按引用范围定位，同一个句柄支持重复及交错迭代，不依赖上次游标位置。
UTF-8 使用严格增量解码；既有消费流核对完整摘要和长度，异常中止报告。
不在 str、property、__html__ 或 Jinja 宏中重新拼接整个片段。

DiffEngine 的 detail_store 默认 None，直接调用仍返回原有 HTML 字符串。
应用工作线程显式传入 store，将 FileDiff 的完整正文 HTML 换成轻量引用；
旧的 retain_text_contents 公共默认值仍保持 True。
单项目、多项目及递归成员共用所属报告任务的 store，子包 VCS 清理不删除片段。
模板使用 file_detail.html 的迭代 include，普通 HTML 字符串路径继续兼容。
最终结果仍是单文件离线 HTML，不包含临时文件引用或本机片段路径。

### C1b：stable renderer 不再先拼出完整表格

stable_diff.iter_table 按既有行顺序产生片段，make_table 保留返回字符串的兼容封装。
应用内部、没有显式渲染限制的路径直接把迭代结果写入 store。
字符匹配、行匹配、精确 LCS 统计均未改变；未知字节和特殊分隔符在匹配后显示。
元数据说明以流前缀保留，不能在添加说明时把整表重新拼回内存。
小修改仍使用原 HtmlDiff；新增/删除等原 HtmlDiff 路径没有为统一接口强行替换。
显式限制策略继续使用原字符串核算路径，不改变其既有契约。

### 生命周期和未扩大范围

报告完整写出后、正式输出提交之前关闭片段存储；异常路径也在 finally 收尾。
多项目不会在前一个项目完成时关闭整个报告的 store。
片段存储只清理自己拥有的目录，不拥有或清理 exporter stage、raw 缓存或 journal。
沿用报告同目录临时写入与事务提交；片段写读错误不能发布半份报告。
增加 comparetool_html_ 临时目录前缀，仍受已有所有权、存活 PID、时间条件控制。
未删除任何跨阶段摘要、HMAC、并发目标保护或归档安全检查。
没有修改 VCS 历史端点算法、导出字节规则和上线指令。

## 针对性验收

C1a 旧相关套件 87 项通过；新存储套件最初 7 项通过。
加入逐行输出及未知字节/特殊字符测试后，C1b 针对性 47 项通过。
原始文件：c1a_existing.json/log、c1a_storage.json/log、c1b_targeted.json/log。
新增 test_html_detail_store.py 验证重复/交错读取、空片段、UTF-8 跨块、
跨 store 引用、内容改写、截断、短写、未完成片段不发布、单/多项目失败保留旧输出，
以及子包 VCS 先清理而报告片段仍可用。
完整回归、独立性能和浏览器最终结果记录于下文及证据 JSON。
