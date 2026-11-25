@echo off
call "%~dp0scripts\run-windows.cmd" status %*
exit /b %errorlevel%
