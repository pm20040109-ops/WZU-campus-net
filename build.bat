@echo off
setlocal
cd /d "%~dp0"
set "PYEXE=%CAMPUS_PYTHON%"
if not defined PYEXE (
    if exist ".venv\Scripts\python.exe" (
        set "PYEXE=%CD%\.venv\Scripts\python.exe"
    ) else (
        set "PYEXE=python"
    )
)
echo [build] using Python: %PYEXE%
"%PYEXE%" -c "import tkinter, PyInstaller"
if errorlevel 1 (
    echo [build] Python must include tkinter and PyInstaller.
    exit /b 1
)
"%PYEXE%" -m unittest discover -s tests -v
if errorlevel 1 exit /b 1
"%PYEXE%" -m PyInstaller --noconfirm CampusNet.spec %*
if errorlevel 1 exit /b 1
echo [build] SUCCESS: CampusNet.exe (default: dist; --distpath overrides)
endlocal
