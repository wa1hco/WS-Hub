@echo off

REM WS Hub
REM Copyright (C) 2026 Jeff Millar, WA1HCO
REM
REM SPDX-License-Identifier: GPL-3.0-or-later
REM
REM Put "WS Hub at logon" in this user's Startup folder (no admin).
REM At logon: read seat intent, start WSJT-X if this is a WSJT seat, start WS Hub.

setlocal EnableExtensions
cd /d "%~dp0"
set "HERE=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNK=%STARTUP%\WS Hub at logon.lnk"
set "VBS=%HERE%Start-WimsAtLogon.vbs"
set "WSCRIPT=%SystemRoot%\System32\wscript.exe"
set "ICO=%HERE%assets\wims.ico"

if not exist "%VBS%" (
  echo ERROR: missing %VBS%
  if /I not "%~1"=="/nopause" pause
  exit /b 1
)
if not exist "%STARTUP%" mkdir "%STARTUP%" 2>nul

echo.
echo  Install WS Hub logon auto-start
echo  =============================
echo  Uses this PC's launcher checkboxes:
echo    %%APPDATA%%\wims\seat_intent.json
echo  WSJT-X intent  -^> start WSJT-X ^(named --rig-name^)
echo  N1MM intent    -^> start N1MM
echo  Always         -^> WS Hub launcher ^(agents / site server from intent^)
echo  Startup:  %LNK%
echo.

if exist "%STARTUP%\WS Hub Seat.lnk" (
  echo  NOTE: "WS Hub Seat.lnk" is also in Startup ^(radio pack^).
  echo  Remove it with Remove-WimsSeatStartup.cmd if you only want this script.
  echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Install-WimsLogonStartup.ps1" -NoPause

if not exist "%LNK%" (
  echo ERROR: could not create Startup shortcut
  if /I not "%~1"=="/nopause" pause
  exit /b 1
)

echo  RESULT: OK — next logon starts from seat intent
echo  Test now:  Start-WimsAtLogon.cmd --dry-run
echo  Remove:    Remove-WimsLogonStartup.cmd
echo.
if /I not "%~1"=="/nopause" pause
exit /b 0
