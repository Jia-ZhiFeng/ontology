@echo off
call "%~dp0run-windows.cmd" stop %*
exit /b %errorlevel%
