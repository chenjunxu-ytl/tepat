@echo off
rem Dev fast-run: start server.py straight from source, no PyInstaller.
setlocal

rem 桌面入口（用户裁决 2026-10-14）：首次启动自动在桌面放 Tepat 快捷方式——
rem bat 无法自带图标，所以放 .lnk（带 assets\icon.ico 企鹅图标）。
rem 两级回退：Tepat.lnk 名字被云同步 tombstone 卡住（建了又删过的名字会这样）
rem 时换 Tepat App.lnk；PowerShell 完全不可用才落到无图标的 bat。
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "TEPAT_DESKTOP=%%D"
if not defined TEPAT_DESKTOP set "TEPAT_DESKTOP=%USERPROFILE%\Desktop"
if not exist "%TEPAT_DESKTOP%\Tepat.lnk" if not exist "%TEPAT_DESKTOP%\Tepat App.lnk" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws = New-Object -ComObject WScript.Shell; foreach ($n in 'Tepat.lnk','Tepat App.lnk') { $lnk = $ws.CreateShortcut((Join-Path $env:TEPAT_DESKTOP $n)); $lnk.TargetPath = '%~dp0run.bat'; $lnk.WorkingDirectory = '%~dp0.'; $lnk.IconLocation = '%~dp0assets\icon.ico'; $lnk.Description = 'Tepat - Malay language checker (local server)'; $lnk.WindowStyle = 7; try { $lnk.Save() } catch { }; if (Test-Path (Join-Path $env:TEPAT_DESKTOP $n)) { break } }" 2>nul
  if exist "%TEPAT_DESKTOP%\Tepat.lnk" (
    echo [tepat] desktop shortcut created: %TEPAT_DESKTOP%\Tepat.lnk
  ) else if exist "%TEPAT_DESKTOP%\Tepat App.lnk" (
    echo [tepat] desktop shortcut created: %TEPAT_DESKTOP%\Tepat App.lnk
  ) else (
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
