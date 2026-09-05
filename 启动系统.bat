@echo off
title 数据查看器
cd /d "%~dp0"

echo ============================================
echo   数据查看器
echo ============================================
echo.
echo   地址: http://localhost:8300
echo   关闭本窗口即停止服务
echo.

start "" http://localhost:8300

"D:\Program Files\WPS Comate\scripts\apps\basic\tools\python\versions\3.12.12\python.exe" server.py

echo.
echo 服务已停止。
pause
