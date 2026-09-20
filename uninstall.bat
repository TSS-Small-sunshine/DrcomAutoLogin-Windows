@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ============================================================
REM   uninstall.bat - 移除 Dr.COM 自动登录服务 (v2.1)
REM   修复: UTF-8 BOM + chcp 65001（修复 cmd 中文编码问题）
REM ============================================================

REM 把脚本自身路径放到环境变量，供 PowerShell 读取
set "INSTALL_BAT_PATH=%~f0"

REM ============================================================
REM   自提升到管理员
REM ============================================================
net session >nul 2>&1
if errorlevel 1 (
    echo [INFO] 当前非管理员，重启提升权限...
    set "ELEVATE_PS1_NAME=drcom_elevate_%RANDOM%.ps1"
    powershell -NoProfile -Command "try { $ErrorActionPreference='Stop'; $p = Join-Path ([IO.Path]::GetTempPath()) $env:ELEVATE_PS1_NAME; $body = 'Start-Process -FilePath $args[0] -Verb RunAs'; [IO.File]::WriteAllText($p, $body, (New-Object Text.UTF8Encoding $true)); Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',$p,$env:INSTALL_BAT_PATH } catch { Write-Host ('ELEVATE_FAIL: ' + $_.Exception.Message); exit 1 }"
    exit /b
)

set "SCRIPT_DIR=%~dp0"
if "!SCRIPT_DIR:~-1!"=="\" set "SCRIPT_DIR=!SCRIPT_DIR:~0,-1!"
set "NSSM=!SCRIPT_DIR!\tools\nssm.exe"
set "LOG_DIR=!SCRIPT_DIR!\logs"

echo.
echo ============================================================
echo   Dr.COM 自动登录服务 - 卸载 (v2.0)
echo ============================================================
echo   脚本目录: !SCRIPT_DIR!
echo ============================================================
echo.

REM ============================================================
REM   停止 + 删除服务
REM ============================================================
if exist "!NSSM!" (
    "!NSSM!" stop DrcomAutoLogin >nul 2>&1
    "!NSSM!" remove DrcomAutoLogin confirm >nul 2>&1
    echo [INFO] 已调用 NSSM 移除服务（忽略错误信息）
) else (
    echo [WARN] 未找到 NSSM: !NSSM!
    echo [WARN] 仍将尝试通过系统命令移除服务...
)
sc stop DrcomAutoLogin >nul 2>&1
sc delete DrcomAutoLogin >nul 2>&1

REM ============================================================
REM   删除桌面快捷方式（cmd 原生命令，BOM+chcp 65001 下中文路径 OK）
REM ============================================================
if exist "%USERPROFILE%\Desktop\Dr.COM 配置.url" (
    del /F /Q "%USERPROFILE%\Desktop\Dr.COM 配置.url"
    echo [INFO] 已删除桌面快捷方式
) else (
    echo [INFO] 桌面快捷方式不存在，跳过
)

REM ============================================================
REM   删除开始菜单快捷方式
REM ============================================================
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Dr.COM 配置.url" (
    del /F /Q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Dr.COM 配置.url"
    echo [INFO] 已删除开始菜单快捷方式
) else (
    echo [INFO] 开始菜单快捷方式不存在，跳过
)

REM ============================================================
REM   询问：是否同时删除配置 / 密码 / 日志
REM ============================================================
echo.
echo ============================================================
echo   是否同时删除以下用户数据?
echo     - config.json     (运行配置)
echo     - password.txt    (校园网密码)
echo     - logs\           (所有日志)
echo.
echo   选择:
echo     Y = 是，一并删除
echo     N = 否，保留 (默认)
echo ============================================================
choice /C YN /N /M "删除用户数据？(Y/N, 默认 N)"
set "REMOVE_DATA=%ERRORLEVEL%"
echo.

if "!REMOVE_DATA!"=="1" (
    if exist "!SCRIPT_DIR!\config.json" del /F /Q "!SCRIPT_DIR!\config.json"
    if exist "!SCRIPT_DIR!\password.txt" del /F /Q "!SCRIPT_DIR!\password.txt"
    if exist "!LOG_DIR!" rd /s /q "!LOG_DIR!"
    echo [INFO] 用户数据已删除
) else (
    echo [INFO] 用户数据已保留
    if exist "!SCRIPT_DIR!\config.json" echo        - !SCRIPT_DIR!\config.json
    if exist "!SCRIPT_DIR!\password.txt" echo       - !SCRIPT_DIR!\password.txt
    if exist "!LOG_DIR!" echo                       - !LOG_DIR!\
)

echo.
echo ============================================================
echo   卸载完成！
echo.
echo   如需彻底清理:
echo     - 删除整个目录: !SCRIPT_DIR!
echo ============================================================
echo.
pause
endlocal