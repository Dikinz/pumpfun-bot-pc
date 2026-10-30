@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" perf-check
if errorlevel 1 echo Performance check failed. See results/performance_v87.json
pause
