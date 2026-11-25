@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\windows_runtime.py" %*
  exit /b
)
where py.exe >nul 2>nul
if not errorlevel 1 (
  py.exe -3 "scripts\windows_runtime.py" %*
  exit /b
)
where python.exe >nul 2>nul
if not errorlevel 1 (
  python.exe "scripts\windows_runtime.py" %*
  exit /b
)
echo Python 3.10 or newer is required. Install Python and run start.cmd again.
exit /b 1
