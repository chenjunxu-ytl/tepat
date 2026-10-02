@echo off
rem tepat build — single exe: words + bigrams + blacklist + web UI + tray icon
pip install pyinstaller pillow --quiet
python -m PyInstaller --onefile --noconsole --name tepat ^
  --icon assets\icon.ico ^
  --add-data "words.txt;." ^
  --add-data "bigrams.txt.xz;." ^
  --add-data "blacklist.json;." ^
  --add-data "web;web" ^
  --add-data "assets;assets" ^
  --add-data "rules.json;." ^
  server.py
echo.
echo Build output: dist\tepat.exe
pause
