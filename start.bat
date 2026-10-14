@echo off
rem Source-run launcher (no exe, no PyInstaller, no code-signing issues).
rem Tepat's server is pure Python stdlib — any Python 3.10+ works, nothing to pip install.
setlocal
cd /d "%~dp0"

rem 桌面入口（用户裁决 2026-10-14）：首次启动自动在桌面放 Tepat 快捷方式——
rem bat 无法自带图标，所以放 .lnk（带 assets\icon.ico 企鹅图标）。
rem 两级回退：Tepat.lnk 名字被云同步 tombstone 卡住（建了又删过的名字会这样）
rem 时换 Tepat App.lnk；PowerShell 完全不可用才落到无图标的 bat。
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "TEPAT_DESKTOP=%%D"
if not defined TEPAT_DESKTOP set "TEPAT_DESKTOP=%USERPROFILE%\Desktop"
if not exist "%TEPAT_DESKTOP%\Tepat.lnk" if not exist "%TEPAT_DESKTOP%\Tepat App.lnk" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws = New-Object -ComObject WScript.Shell; foreach ($n in 'Tepat.lnk','Tepat App.lnk') { $lnk = $ws.CreateShortcut((Join-Path $env:TEPAT_DESKTOP $n)); $lnk.TargetPath = '%~dp0start.bat'; $lnk.WorkingDirectory = '%~dp0.'; $lnk.IconLocation = '%~dp0assets\icon.ico'; $lnk.Description = 'Tepat - Malay language checker (local server)'; $lnk.WindowStyle = 7; try { $lnk.Save() } catch { }; if (Test-Path (Join-Path $env:TEPAT_DESKTOP $n)) { break } }" 2>nul
  if exist "%TEPAT_DESKTOP%\Tepat.lnk" (
    echo [tepat] desktop shortcut created: %TEPAT_DESKTOP%\Tepat.lnk
  ) else if exist "%TEPAT_DESKTOP%\Tepat App.lnk" (
    echo [tepat] desktop shortcut created: %TEPAT_DESKTOP%\Tepat App.lnk
  ) else (
    > "%TEPAT_DESKTOP%\tepat.bat" (
      echo @echo off
      echo start "" /min "%~dp0start.bat" %%*
    )
    echo [tepat] desktop launcher created: %TEPAT_DESKTOP%\tepat.bat
  )
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
