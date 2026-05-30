@echo off
setlocal
chcp 65001 >nul
echo ============================================
echo  CompareTool - PyInstaller build
echo ============================================
echo.

set "PYTHON_CMD="

py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    python --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
        "%LOCALAPPDATA%\Python\bin\python.exe" --version >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_CMD="%LOCALAPPDATA%\Python\bin\python.exe""
        )
    )
)

if not defined PYTHON_CMD (
    echo Python was not found.
    echo Please install Python or add it to PATH, then run build.bat again.
    pause
    exit /b 1
)

echo Python: %PYTHON_CMD%
echo.

REM Check whether PyInstaller is installed.
%PYTHON_CMD% -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [1/2] Installing PyInstaller...
    %PYTHON_CMD% -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo Failed to install PyInstaller.
        pause
        exit /b 1
    )
) else (
    echo [1/2] PyInstaller already installed
)

echo.
echo [2/2] Building...

%PYTHON_CMD% -m PyInstaller ^
    --onefile ^
    --console ^
    --name "CompareTool" ^
    --icon "assets\icons\app.ico" ^
    --add-data "templates;templates" ^
    --add-data "assets;assets" ^
    --clean ^
    main.py

if errorlevel 1 (
    echo.
    echo ============================================
    echo  Build failed!
    echo ============================================
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Build finished!
echo  Output: dist\CompareTool.exe
echo ============================================
pause
