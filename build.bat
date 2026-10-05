@echo off
setlocal
rem Evidence stays outside the exe: no multi-gigabyte extraction at each startup.
rem Usage: build.bat [core^|full]  (default full)
rem   core = PRPM-only runtime, no grammar data (rules/indo_words/evidence)
rem   full = PRPM runtime + grammar data pack
set MODE=full
if not "%~1"=="" set MODE=%~1
if /i not "%MODE%"=="core" if /i not "%MODE%"=="full" (
  echo Unknown mode: %MODE% ^(use core or full^)
  exit /b 1
)
echo Building tepat-v2 (mode=%MODE%) ...

python sync_indo.py
if errorlevel 1 exit /b 1

if /i "%MODE%"=="full" if not exist data\evidence.sqlite (
  echo Build evidence first: python build_evidence.py --review-root ..\puzzle\corpus\clean-review\20261002-v3
  exit /b 1
)

rem Tests use their own fixture evidence; they do not read data\evidence.sqlite.
python -m unittest test_evidence test_checker -q
if errorlevel 1 exit /b 1

rem core mode: grammar configs are NOT bundled into _internal; server.py then
rem starts PRPM-only and prepare_release.py --mode core refuses otherwise.
set GRAMMAR_DATA=--add-data "rules.json;." --add-data "indo_words.json;."
if /i "%MODE%"=="core" set GRAMMAR_DATA=

python -m PyInstaller --onedir --noconsole --noconfirm --name tepat-v2 ^
  --icon assets\icon.ico ^
  %GRAMMAR_DATA% ^
  --add-data "web;web" ^
  --add-data "extension\results.js;extension" ^
  --add-data "assets;assets" ^
  server.py
if errorlevel 1 exit /b 1

if /i "%MODE%"=="core" (
  if exist dist\tepat-v2\_internal\rules.json (
    echo core build unexpectedly contains rules.json - refusing
    exit /b 1
  )
  if exist dist\tepat-v2\_internal\indo_words.json (
    echo core build unexpectedly contains indo_words.json - refusing
    exit /b 1
  )
  echo Build output: dist\tepat-v2\tepat-v2.exe ^(core, PRPM-only^)
) else (
  if not exist dist\tepat-v2\_internal\data mkdir dist\tepat-v2\_internal\data
  copy /y data\evidence.sqlite dist\tepat-v2\_internal\data\evidence.sqlite >nul
  copy /y rules.json dist\tepat-v2\rules.json >nul
  copy /y indo_words.json dist\tepat-v2\indo_words.json >nul
  echo Build output: dist\tepat-v2\tepat-v2.exe ^(full^)
)
echo Keep the entire tepat-v2 folder together.
