@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" amm-check
if errorlevel 1 echo AMM check not passed. See app/data/target_amm_check_v59.json
pause
