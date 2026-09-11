# 使用说明书维护

面向使用者的正式文档是 [CompareTool 使用说明书](../CompareTool_使用说明书.pdf)。`build_manual.py` 是内容与排版的唯一维护源；`dist/CompareTool_使用说明书.pdf` 是交付副本，不单独编辑。

## 更新与验证

使用 Windows 的 Microsoft YaHei 字体，以及带有 `reportlab`、`pypdf`、`pdfplumber` 的 Python 环境。优先复用可用文档运行时；这些包仅用于维护说明书，不是应用或 EXE 打包依赖。

在项目根目录执行：

```powershell
python docs/manual/build_manual.py
python docs/manual/verify_pdf.py
```

生成脚本先写入 `.tmp/manual/`，核对分页和内容结构后复制到 `docs` 和 `dist`。校验脚本检查两份 PDF 字节一致、12 页、目录跳转、书签、字体嵌入、缺失字形和文本越界。调整章节或页数时同步更新验证预期；结构检查不能代替视觉检查。

有 Poppler 时渲染后逐页检查，尤其关注修改页的表格、换行和页脚：

```powershell
pdftoppm -r 120 -png docs/CompareTool_使用说明书.pdf .tmp/manual/page
```

正文或界面操作变化时更新源稿及页脚日期，并把源稿和正式 PDF 一起提交。临时校验记录、提取文本和 PNG 留在 `.tmp/manual/`。`build.bat` 只复制已验证的正式 PDF 到 `dist`，不重新排版；分发时将 PDF 与 EXE 放在一起。
