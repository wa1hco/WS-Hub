@echo off
REM Older name for "Add WS Hub to Desktop.bat". Calls that file.
setlocal EnableExtensions
cd /d "%~dp0"
call "%~dp0Add WS Hub to Desktop.bat"
exit /b %ERRORLEVEL%
