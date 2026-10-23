@echo off
setlocal
cd /d "%~dp0app"
"..\.venv\Scripts\python.exe" target_amm_check.py
if errorlevel 1 echo AMM check not passed. See app/data/target_amm_check_v59.json
pause
