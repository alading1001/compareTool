@echo off
chcp 65001 >nul
echo ============================================
echo  代码比对报告工具 - PyInstaller 打包
echo ============================================
echo.

REM 检查 pyinstaller 是否已安装
python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [1/2] 安装 PyInstaller...
    python -m pip install pyinstaller
) else (
    echo [1/2] PyInstaller 已安装
)

echo.
echo [2/2] 开始打包...

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "CompareTool" ^
    --add-data "templates;templates" ^
    --add-data "paichu.txt;." ^
    --clean ^
    main.py

echo.
echo ============================================
echo  打包完成!
echo  输出文件: dist\CompareTool.exe
echo ============================================
pause
