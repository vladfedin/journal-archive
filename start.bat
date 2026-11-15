@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -c "import requests, bleach, defusedxml, jinja2" >nul 2>nul
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -e .
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m ljarchive %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Could not start. Install Python 3.10 or newer, then try again.
pause
exit /b 1
