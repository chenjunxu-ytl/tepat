@echo off
rem Source-run launcher (no exe, no PyInstaller, no code-signing issues).
rem Tepat's server is pure Python stdlib — any Python 3.10+ works, nothing to pip install.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [tepat] Python not found.
  echo Install it from the Microsoft Store ^(search "Python 3.13"^) or https://python.org
  echo then run this file again.
  pause
  exit /b 1
)

rem 循环运行（2026-10-12 self-update）：server 更新自身后写 .restart-pending
rem 再退出——这里看到标记就删标记并用新代码重启；正常退出（用户关）则结束。
:run
echo [tepat] starting... UI at http://127.0.0.1:8377 (tray icon appears; right-click to open)
python server.py %*
if exist ".restart-pending" (
  del ".restart-pending"
  echo [tepat] update applied — restarting with the new version...
  timeout /t 2 /nobreak >nul
  goto run
)
