@echo off
setlocal
cd /d "%~dp0"
title Minijob Finder

rem 1) Already running? Then just open the browser.
curl -s -o nul http://127.0.0.1:5000/api/ollama && (start http://127.0.0.1:5000 & exit /b 0)

rem 2) First start: create the venv and install packages.
if not exist ".venv\Scripts\python.exe" (
  echo Einrichtung beim ersten Start ...
  python -m venv .venv || (echo Python fehlt: winget install -e --id Python.Python.3.12 & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)
)

rem 3) Start Ollama if it isn't running (without it the template is used).
curl -s -o nul http://localhost:11434 || (
  where ollama >nul 2>nul && (echo Starte Ollama ... & start "" /min ollama serve)
)

rem 4) Open the browser after 3 seconds, run the server in this window.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5000"
echo Minijob Finder laeuft. Fenster offen lassen - schliessen beendet die App.
".venv\Scripts\python.exe" app.py
pause