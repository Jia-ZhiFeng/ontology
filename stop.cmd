@echo off
call "%~dp0scripts\stop-windows.cmd" %*
exit /b %errorlevel%
