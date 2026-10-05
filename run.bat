@echo off
rem Dev fast-run: start server.py straight from source, no PyInstaller.
rem Usage: run.bat [--core]   (core = hide data\evidence.sqlite so server starts PRPM-only)
setlocal
set TEASY_ROOT=%~dp0
if /i "%~1"=="--core" (
  if exist data\evidence.sqlite ren data\evidence.sqlite evidence.sqlite.off
  echo [run] core mode: evidence hidden; restore with "run.bat --restore"
)
if /i "%~1"=="--restore" (
  if exist data\evidence.sqlite.off ren data\evidence.sqlite.off evidence.sqlite
  echo [run] full mode restored
)
python server.py %*
