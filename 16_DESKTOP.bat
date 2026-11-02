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
rem Desktop window: own environment .venv-ui. WebView2Loader.dll and .pyd files do not load from
rem long paths (even with LongPathsEnabled), so this folder may have at most 150 characters.
set "DESKTOP_ROOT=%~dp0"
if not "%DESKTOP_ROOT:~151,1%"=="" goto long_path
rem Install only when the pinned requirements differ from the installed copy (first run, update, broken install).
if exist ".venv-ui\Scripts\pythonw.exe" fc /b "desktop\requirements-desktop.txt" ".venv-ui\requirements-desktop.installed" >nul 2>nul && goto ready
rem Never install into a changed release or a foreign .venv (the window repeats the release check at every start).
"%~dp0.venv\Scripts\python.exe" -I "%~dp0app\bootstrap.py" verify-release || goto failed
if not exist ".venv-ui\Scripts\pythonw.exe" (
  "%~dp0.venv\Scripts\python.exe" -I -m venv "%~dp0.venv-ui" || goto failed
)
"%~dp0.venv-ui\Scripts\python.exe" -I -m pip install --disable-pip-version-check --require-hashes -r "%~dp0desktop\requirements-desktop.txt" || goto failed
copy /y "desktop\requirements-desktop.txt" ".venv-ui\requirements-desktop.installed" >nul || goto failed
:ready
if /i "%~1"=="--prepare" exit /b 0
start "" "%~dp0.venv-ui\Scripts\pythonw.exe" -I "%~dp0desktop\app_main.py" || goto failed
exit /b 0
:long_path
echo The folder path is too long for the desktop window (WebView2 needs at most 150 characters):
echo "%~dp0"
echo Extract the bot into a short folder, for example %LOCALAPPDATA%\PumpfunBot, then run 16_DESKTOP.bat again.
goto failed
:failed
echo Failed. Keep this window and copy the error text.
pause
exit /b 1
