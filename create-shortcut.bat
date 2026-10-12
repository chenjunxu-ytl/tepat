@echo off
rem Create a desktop shortcut to Tepat with the tepat icon (user decision
rem 2026-10-12: bat files cannot embed icons themselves; a shortcut carries
rem the icon for both the desktop and the taskbar window).
setlocal
cd /d "%~dp0"

if not exist "server.py" (
  echo [tepat] run this file from the Tepat source folder ^(beside server.py^).
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "create-shortcut.ps1"

if errorlevel 1 (
  echo [tepat] shortcut creation failed - you can still start Tepat with start.bat
  pause
  exit /b 1
)
echo [tepat] done. Start Tepat from the "Tepat" desktop shortcut from now on.
pause
