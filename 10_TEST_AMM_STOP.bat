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
echo Controlled PAPER test: stopping only the current AMM branch. Primary should continue.
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" test-amm-stop
set "CHECK_CODE=%ERRORLEVEL%"
if not "%CHECK_CODE%"=="0" echo Injection failed. Keep this window and copy the error.
pause
exit /b %CHECK_CODE%
