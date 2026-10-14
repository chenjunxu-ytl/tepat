@echo off
rem Dev fast-run: start server.py straight from source, no PyInstaller.
setlocal

rem 桌面入口（用户裁决 2026-10-14）：首次启动自动在桌面放 Tepat 快捷方式——
rem bat 无法自带图标，所以放 .lnk（指向 start.bat，带 assets\icon.ico 企鹅图标），
rem 双击即启动本目录的 server。已存在则不覆盖。
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "TEPAT_DESKTOP=%%D"
if not defined TEPAT_DESKTOP set "TEPAT_DESKTOP=%USERPROFILE%\Desktop"
if not exist "%TEPAT_DESKTOP%\Tepat.lnk" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut((Join-Path $env:TEPAT_DESKTOP 'Tepat.lnk')); $lnk.TargetPath = '%~dp0start.bat'; $lnk.WorkingDirectory = '%~dp0.'; $lnk.IconLocation = '%~dp0assets\icon.ico'; $lnk.Description = 'Tepat - Malay language checker (local server)'; $lnk.WindowStyle = 7; $lnk.Save()" 2>nul
  if exist "%TEPAT_DESKTOP%\Tepat.lnk" (
    echo [tepat] desktop shortcut created: %TEPAT_DESKTOP%\Tepat.lnk
  ) else (
    rem PowerShell 失败（策略限制等）——退回无图标的 bat 入口
    > "%TEPAT_DESKTOP%\tepat.bat" (
      echo @echo off
      echo start "" /min "%~dp0run.bat" %%*
    )
    echo [tepat] desktop launcher created: %TEPAT_DESKTOP%\tepat.bat
  )
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
