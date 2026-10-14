@echo off
rem Source-run launcher (no exe, no PyInstaller, no code-signing issues).
rem Tepat's server is pure Python stdlib — any Python 3.10+ works, nothing to pip install.
setlocal
cd /d "%~dp0"

rem 桌面入口（用户裁决 2026-10-14）：首次启动自动在桌面放 tepat.bat——
rem 双击即启动本目录的 server（绝对路径，不怕从哪里调）。已存在则不覆盖。
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "TEPAT_DESKTOP=%%D"
if not defined TEPAT_DESKTOP set "TEPAT_DESKTOP=%USERPROFILE%\Desktop"
if not exist "%TEPAT_DESKTOP%\tepat.bat" (
  > "%TEPAT_DESKTOP%\tepat.bat" (
    echo @echo off
    echo start "" /min "%~dp0%~nx0" %%*
  )
  echo [tepat] desktop shortcut created: %TEPAT_DESKTOP%\tepat.bat
)
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
