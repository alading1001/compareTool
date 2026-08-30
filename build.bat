@echo off
setlocal
chcp 65001 >nul
echo ============================================
echo  CompareTool - PyInstaller build
echo ============================================
echo.

set "PYTHON_CMD="
set "BASE_PYTHON="

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=".venv\Scripts\python.exe""
)

if not defined PYTHON_CMD (
    py -3.12 --version >nul 2>&1
    if not errorlevel 1 (
        set "BASE_PYTHON=py -3.12"
    )
)

if not defined PYTHON_CMD if not defined BASE_PYTHON (
    py -3 --version >nul 2>&1
    if not errorlevel 1 (
        set "BASE_PYTHON=py -3"
    )
)

if not defined PYTHON_CMD if not defined BASE_PYTHON (
    python --version >nul 2>&1
    if not errorlevel 1 (
        set "BASE_PYTHON=python"
    )
)

if not defined PYTHON_CMD if not defined BASE_PYTHON (
    if exist "%LOCALAPPDATA%\Python\bin\python.exe" (
        "%LOCALAPPDATA%\Python\bin\python.exe" --version >nul 2>&1
        if not errorlevel 1 (
            set "BASE_PYTHON="%LOCALAPPDATA%\Python\bin\python.exe""
        )
    )
)

if not defined PYTHON_CMD if not defined BASE_PYTHON (
    echo Python was not found.
    echo Please install Python or add it to PATH, then run build.bat again.
    pause
    exit /b 1
)

if not defined PYTHON_CMD (
    echo Creating project virtual environment...
    %BASE_PYTHON% -m venv .venv
    if errorlevel 1 (
        echo Failed to create project virtual environment.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=".venv\Scripts\python.exe""
)

echo Python: %PYTHON_CMD%
echo.

REM Check whether all build dependencies are installed.
set "INSTALL_DEPS="
%PYTHON_CMD% -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    set "INSTALL_DEPS=1"
)
%PYTHON_CMD% -m pip show jinja2 >nul 2>&1
if errorlevel 1 (
    set "INSTALL_DEPS=1"
)

if defined INSTALL_DEPS (
    echo [1/2] Installing build dependencies...
    %PYTHON_CMD% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Failed to install build dependencies.
        pause
        exit /b 1
    )
) else (
    echo [1/2] Build dependencies already installed
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
