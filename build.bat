@echo off
setlocal

set "BUILD_MODE=%~1"
if "%BUILD_MODE%"=="" set "BUILD_MODE=full"
if /I not "%BUILD_MODE%"=="core" if /I not "%BUILD_MODE%"=="full" (
  echo Usage: build.bat [core^|full]
  exit /b 2
)

echo Building Tepat mode: %BUILD_MODE%

if /I "%BUILD_MODE%"=="full" (
  python sync_indo.py
  if errorlevel 1 exit /b 1
  if not exist data\evidence.sqlite (
    echo Build evidence first: python build_evidence.py --review-root ..\puzzle\corpus\clean-review\20261002-v3
    exit /b 1
  )
  python -m unittest test_evidence test_checker -q
  if errorlevel 1 exit /b 1
) else (
  python -m unittest test_core -q
  if errorlevel 1 exit /b 1
)

set "PYI_DATA=--add-data web;web --add-data extension\results.js;extension --add-data assets;assets"
if /I "%BUILD_MODE%"=="full" set "PYI_DATA=%PYI_DATA% --add-data indo_words.json;. --add-data rules.json;."

python -m PyInstaller --onedir --noconsole --name tepat-v2 ^
  --icon assets\icon.ico ^
  %PYI_DATA% ^
  server.py
if errorlevel 1 exit /b 1

if /I "%BUILD_MODE%"=="full" (
  if not exist dist\tepat-v2\_internal\data mkdir dist\tepat-v2\_internal\data
  copy /y data\evidence.sqlite dist\tepat-v2\_internal\data\evidence.sqlite >nul
  copy /y rules.json dist\tepat-v2\rules.json >nul
  copy /y indo_words.json dist\tepat-v2\indo_words.json >nul
)

echo Build output: dist\tepat-v2\tepat-v2.exe
if /I "%BUILD_MODE%"=="core" (
  echo Lightweight PRPM build: grammar data is not bundled.
) else (
  echo Full build: grammar data is bundled.
)
echo Keep the entire tepat-v2 folder together.
