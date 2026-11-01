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
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" amm-check
set "CHECK_CODE=%ERRORLEVEL%"
if not "%CHECK_CODE%"=="0" echo AMM check not passed. See app/data/target_amm_check_v59.json
pause
exit /b %CHECK_CODE%
