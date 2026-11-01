@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not "%errorlevel%"=="0" goto failed
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" install
if not "%errorlevel%"=="0" goto failed
pause
exit /b 0
:failed
echo Failed. Keep this window and copy the error text.
pause
exit /b 1
