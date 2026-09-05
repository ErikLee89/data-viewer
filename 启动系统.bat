@echo off
title 旋筒风帆运行数据分析系统
cd /d "%~dp0"

echo ============================================
echo   旋筒风帆运行数据分析系统
echo ============================================
echo.

if not exist "data\data.parquet" (
    echo [首次运行] 正在导入数据，约需 1 分钟...
    "D:\Program Files\WPS Comate\scripts\apps\basic\tools\python\versions\3.12.12\python.exe" import_data.py
    if errorlevel 1 (
        echo.
        echo [错误] 数据导入失败，请检查源 Excel 文件是否存在。
        pause
        exit /b 1
    )
    echo.
)

echo 正在启动服务...
echo.
echo   地址: http://localhost:8300
echo   关闭本窗口即停止服务
echo.
echo --------------------------------------------

start "" http://localhost:8300

"D:\Program Files\WPS Comate\scripts\apps\basic\tools\python\versions\3.12.12\python.exe" server.py

echo.
echo 服务已停止。
pause
