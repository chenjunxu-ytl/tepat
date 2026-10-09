@echo off
setlocal
rem Single release mode (2026-10-09): rules + word lists + PRPM. The legacy
rem 2.3GB evidence corpus is no longer built or bundled — checks run entirely
rem from APPDATA rule files plus PRPM lookups.
echo Building tepat-v2 ...

python tools\sync_indo.py
if errorlevel 1 exit /b 1

rem Tests use their own fixture evidence; they do not read data\evidence.sqlite.
python -m unittest discover -s tests -p "test_*.py" -q
if errorlevel 1 exit /b 1

rem rules.json / indo_words.json ride along as seed values: on first start
rem _config_path() copies them to %APPDATA%\tepat, where all runtime writes go.
python -m PyInstaller --onedir --noconsole --noconfirm --name tepat-v2 ^
  --icon assets\icon.ico ^
  --add-data "rules.json;." --add-data "indo_words.json;." ^
  --add-data "web;web" ^
  --add-data "extension\results.js;extension" ^
  --add-data "assets;assets" ^
  server.py
if errorlevel 1 exit /b 1

copy /y rules.json dist\tepat-v2\rules.json >nul
copy /y indo_words.json dist\tepat-v2\indo_words.json >nul
echo Build output: dist\tepat-v2\tepat-v2.exe
echo Keep the entire tepat-v2 folder together.
