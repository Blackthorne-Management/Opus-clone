@echo off
rem Double-click to run the Auto-Clipper in this window (close the window to stop it).
rem If you chose "start automatically" during install, it is already running in the background.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Install.bat first.
  pause
  exit /b 1
)
rem Opens the dashboard at the port in your settings once the app is up.
start "" /b ".venv\Scripts\python.exe" -m kir_clipper open --wait 60
".venv\Scripts\python.exe" -m kir_clipper run
pause
