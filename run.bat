@echo off
rem Dev fast-run: start server.py straight from source, no PyInstaller.
setlocal

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
set TEASY_ROOT=%~dp0
rem 循环运行（与 start.bat 同款）：restart 标记（tray Restart / /api/restart /
rem self-update）触发 server 退出——这里删标记并重启；正常退出则结束。
:run
python server.py %*
if exist ".restart-pending" (
  del ".restart-pending"
  echo [tepat] restart requested — relaunching...
  timeout /t 1 /nobreak >nul
  goto run
)
