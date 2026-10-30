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
echo Stop the bot normally before this test: the provider allows one socket.
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" network-check
set "CHECK_CODE=%ERRORLEVEL%"
if not "%CHECK_CODE%"=="0" echo Review results/network_check_v89.json. PARTIAL means the backup path is not verified.
pause
exit /b %CHECK_CODE%
