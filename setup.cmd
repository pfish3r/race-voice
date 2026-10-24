@echo off
setlocal
cd /d "%~dp0"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.12 x64 from https://www.python.org/downloads/windows/ then rerun setup.
  pause
  exit /b 1
)
py -3.12 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Setup complete. Run Start-Race-Voice.cmd.
pause
exit /b 0
:fail
echo Setup failed. Check the error above and your internet connection.
pause
exit /b 1
