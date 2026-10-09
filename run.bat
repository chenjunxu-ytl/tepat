@echo off
rem Dev fast-run: start server.py straight from source, no PyInstaller.
setlocal
set TEASY_ROOT=%~dp0
python server.py %*
