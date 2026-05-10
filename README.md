# 代码比对报告工具

Windows 桌面应用，输入 Git/SVN 项目路径和两个版本号，一键生成 HTML 差异报告并导出变更文件。

## 功能

- 支持 **Git** 和 **SVN** 两种版本控制系统
- 输入新旧版本号，自动生成**左右分栏对比**的 HTML 报告
- 左侧文件树，按颜色标记：🟢 新增 / 🔵 修改 / 🔴 删除
- 支持目录折叠展开，一键全部展开/折叠
- 点击文件查看代码差异（新增/修改/删除行高亮）
- **变更清单弹窗**，按新增/修改/删除分类列出所有变更文件
- 自动导出新版本和旧版本的变更文件（按项目名分层目录结构）
- 支持 **glob 排除规则**（`*.class`、`target/**` 等），避免编译产物干扰
- 二进制/归档文件（`.jar`、`.war`、`.class` 等）标记变更但不展示内容
- 中文编码自动适配（UTF-8 → GBK 回退）
- 配置自动记忆，下次打开恢复上次设置
- 打包成单文件 exe，无需 Python 环境

## 运行

### 开发环境

```bash
pip install jinja2
python main.py
```

### 打包为 exe

```bash
build.bat
```

打包后的 exe 在 `dist/CompareTool.exe`，可直接分发使用。

## 排除规则

项目目录下的 `paichu.txt` 定义默认排除规则，每行一个 glob 模式：

```
*.class
target/**
.git/**
.idea/**
node_modules/**
...
```

规则语法：
- `*.class` — 匹配任意深度的 .class 文件
- `target/**` — 匹配 target 目录下所有文件
- `**/test/**` — 匹配任意位置的 test 目录

## 配置文件

`compareTool_config.json` 自动保存上次的项目路径、VCS 类型、输出路径等设置。
