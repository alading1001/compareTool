# 窗口流程计时诊断

`tools/diagnose_folder_gui.py` 是独立诊断入口，不修改主程序的比较规则。
它建立单独配置、临时根、事务密钥和输出目录，通过原窗口的 `_generate()`
进入真实工作线程。最终完成提示改为写日志并关闭本次诊断窗口。
正式源码、用户配置、原 EXE 和已有交付输出不被此脚本替换。

输入 JSON 字段：`old`、`new`、`project_name`、`exclude_patterns`、
`show_full_context`。输入目录只作为读取端点，不允许将输出目录放进输入树。
输出必须是尚不存在的新目录，避免误覆盖。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tools\diagnose_folder_gui.py --inputs 输入.json --output 新诊断目录 --mode gui
```

输出内容：
- `events.jsonl`：阶段开始/结束、窗口回调计数、完成与错误信息。
- `summary.json`：本次结果、工作线程耗时、各阶段累计时间。
- `application.log`：本次运行的应用告警。
- `delivery`：本次独立报告及新旧变更文件；`runtime`：本次临时根。

阶段存在嵌套，不能把所有累计值直接相加。例如 `generate_diff` 包含目录扫描。
计时不等于冷启动总时间，也不能用于证明不同运行时磁盘缓存状态完全一致。
业务路径放在本机输入 JSON，不写入这个可提交的诊断脚本。

## 本机真实目录诊断（2026-09-26）

同一对输入共 33 个变更，完整明细、不设排除规则。
源码 GUI 工作线程本次 80.55 秒；独立诊断 EXE 的 GUI 工作线程本次 31.20 秒。
两次均成功，新旧交付文件合计 66 份，逐文件 SHA-256 核对一致。
诊断 EXE 使用桌面全新输出目录，没有访问或恢复原 `444` 输出根的事务。
本次没有重现用户报告的 5 分 50 秒，不能声称根因已定位或问题已修复。
正式应用源码、已有 EXE、用户配置本次未修改；只新增独立诊断脚本及文档。
原始记录位于 `.tmp/gui_timing_e7a05t4u/`，汇总为 `comparison.json`、
`export-verification.json`；源码 GUI 记录在 `source_gui/`。
EXE 记录在桌面 `CompareTool_diagnostic_e7a05t4u/` 下的 `events.jsonl` 和 `summary.json`。
首次通过 REPL 启动的打包子进程停在依赖分析，已仅终止该次自建构建进程树；
随后通过普通终端完成打包和实际窗口生成，未修改安全设置或进程全局权限。
