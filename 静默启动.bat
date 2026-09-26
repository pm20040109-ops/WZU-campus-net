@echo off
REM 最小化启动（开机自启会用 --minimized 标志，这里提供手动测试入口）
start "" /min "%~dp0dist\CampusNet.exe" --minimized
