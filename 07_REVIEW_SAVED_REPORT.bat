@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run 01_INSTALL.bat first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" review-archive
if errorlevel 1 goto failed
pause
exit /b 0
:failed
echo Failed. Keep this window and copy the error text.
pause
exit /b 1
