@echo off
setlocal
cd /d "%~dp0"
if exist ".artifacts\dist\CampusNet.exe" (
    start "" "%~dp0.artifacts\dist\CampusNet.exe" --demo
    exit /b 0
)
set "PYEXE=%CAMPUS_PYTHON%"
if not defined PYEXE (
    if exist ".venv\Scripts\python.exe" (
        set "PYEXE=%CD%\.venv\Scripts\python.exe"
    ) else (
        set "PYEXE=python"
    )
)
"%PYEXE%" campus_gui.py --demo
if errorlevel 1 pause
