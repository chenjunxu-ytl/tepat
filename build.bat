@echo off
setlocal
rem Single build mode (2026-10-09): runtime + web + extension assets. Rule and
rem word-list data live only in %APPDATA%\tepat; GitHub is the sole initial
rem distribution channel — first launch fetches rules/word lists immediately.
echo Building tepat-v2 ...

rem Tests use their own fixture evidence; they do not read data\evidence.sqlite.
python -m unittest discover -s tests -p "test_*.py" -q
if errorlevel 1 exit /b 1

python -m PyInstaller --onedir --noconsole --noconfirm --name tepat-v2 ^
  --icon assets\icon.ico ^
  --add-data "web;web" ^
  --add-data "extension\results.js;extension" ^
  --add-data "assets;assets" ^
  server.py
if errorlevel 1 exit /b 1

echo Build output: dist\tepat-v2\tepat-v2.exe
echo Keep the entire tepat-v2 folder together.
