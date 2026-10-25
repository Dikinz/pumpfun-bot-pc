@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" recovery-check
if errorlevel 1 echo Recovery check failed. See results/recovery_check_v76.json
pause
