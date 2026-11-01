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
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" recovery-check
set "CHECK_CODE=%ERRORLEVEL%"
if not "%CHECK_CODE%"=="0" echo Recovery check failed. See results/recovery_check_v89.json
pause
exit /b %CHECK_CODE%
