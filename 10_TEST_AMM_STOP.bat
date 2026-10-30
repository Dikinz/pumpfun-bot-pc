@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
cd /d "%~dp0"
echo Controlled PAPER test: stopping only the current AMM branch. Primary should continue.
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" test-amm-stop
if errorlevel 1 echo Injection failed. Keep this window and copy the error.
pause
