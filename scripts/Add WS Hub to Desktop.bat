@echo off
REM Put a WS Hub icon on the Desktop and in the Start menu.
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "%~dp0windows\Install-WIMS-Desktop-Shortcut.cmd" (
  echo This folder does not look like WS Hub.
  echo Open the WS Hub folder and double-click this file again.
  pause
  exit /b 1
)
call "%~dp0windows\Install-WIMS-Desktop-Shortcut.cmd"
exit /b %ERRORLEVEL%
