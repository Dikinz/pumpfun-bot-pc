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
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" perf-check
set "CHECK_CODE=%ERRORLEVEL%"
if not "%CHECK_CODE%"=="0" echo Performance check failed. See results/performance_v87.json
pause
exit /b %CHECK_CODE%
