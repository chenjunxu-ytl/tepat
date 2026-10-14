@echo off
rem Dev fast-run: start server.py straight from source, no PyInstaller.
setlocal

rem 桌面入口（用户裁决 2026-10-14）：首次启动自动在桌面放 Tepat 快捷方式——
rem bat 无法自带图标，所以放 .lnk（带 assets\icon.ico 企鹅图标）。
rem 名字就叫 Tepat.lnk；若该名字被云同步 tombstone 卡住（建了又删过的名字
rem 会这样），先建 Tepat App.lnk 占位，之后每次启动重试升级回 Tepat.lnk，
rem 成功即删占位。PowerShell 完全不可用才落到无图标的 bat。
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "TEPAT_DESKTOP=%%D"
if not defined TEPAT_DESKTOP set "TEPAT_DESKTOP=%USERPROFILE%\Desktop"
if not exist "%TEPAT_DESKTOP%\Tepat.lnk" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$d = $env:TEPAT_DESKTOP; $ws = New-Object -ComObject WScript.Shell; $mk = { param($n) $lnk = $ws.CreateShortcut((Join-Path $d $n)); $lnk.TargetPath = '%~dp0run.bat'; $lnk.WorkingDirectory = '%~dp0.'; $lnk.IconLocation = '%~dp0assets\icon.ico'; $lnk.Description = 'Tepat - Malay language checker (local server)'; $lnk.WindowStyle = 7; try { $lnk.Save() } catch { }; Test-Path (Join-Path $d $n) }; if (& $mk 'Tepat.lnk') { if (Test-Path (Join-Path $d 'Tepat App.lnk')) { Remove-Item (Join-Path $d 'Tepat App.lnk') -Force } } elseif (-not (Test-Path (Join-Path $d 'Tepat App.lnk'))) { & $mk 'Tepat App.lnk' | Out-Null }" 2>nul
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
