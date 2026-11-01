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
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" paper-fault-test --duration 7200 --fault-at 3600 --fault-for 10
set "CHECK_CODE=%ERRORLEVEL%"
pause
exit /b %CHECK_CODE%
