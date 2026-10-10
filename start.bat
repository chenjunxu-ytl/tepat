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

echo [tepat] starting... UI at http://127.0.0.1:8377 (tray icon appears; right-click to open)
python server.py %*
