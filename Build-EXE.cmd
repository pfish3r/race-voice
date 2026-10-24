@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install pyinstaller==6.16.0
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name RaceVoice --collect-all soundcard --hidden-import win32com.client --hidden-import pythoncom app.py
if errorlevel 1 goto fail
echo Copy the WHOLE dist\RaceVoice folder to the other Windows 11 PC.
pause
exit /b 0
:fail
echo Build failed. Read the error above.
pause
exit /b 1
