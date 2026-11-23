@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1 && (py -3 install.py & goto :done)
where python >nul 2>&1 && (python install.py & goto :done)
echo Python 3.9+ is required. Install Python from https://python.org and double-click again.
pause
exit /b 1
:done
if errorlevel 1 pause
