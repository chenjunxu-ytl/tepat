@echo off
rem Evidence stays outside the exe: no multi-gigabyte extraction at each startup.
python sync_indo.py
if errorlevel 1 exit /b 1
if not exist data\evidence.sqlite (
  echo Build evidence first: python build_evidence.py --review-root ..\puzzle\corpus\clean-review\20261002-v3
  exit /b 1
)
python -m unittest test_evidence test_checker -q
if errorlevel 1 exit /b 1
python -m PyInstaller --onedir --noconsole --name tepat-v2 ^
  --icon assets\icon.ico ^
  --add-data "indo_words.json;." ^
  --add-data "web;web" ^
  --add-data "extension\results.js;extension" ^
  --add-data "assets;assets" ^
  --add-data "rules.json;." ^
  server.py
if errorlevel 1 exit /b 1
if not exist dist\tepat-v2\_internal\data mkdir dist\tepat-v2\_internal\data
copy /y data\evidence.sqlite dist\tepat-v2\_internal\data\evidence.sqlite >nul
copy /y rules.json dist\tepat-v2\rules.json >nul
copy /y indo_words.json dist\tepat-v2\indo_words.json >nul
echo Build output: dist\tepat-v2\tepat-v2.exe
echo Keep the entire tepat-v2 folder together.
