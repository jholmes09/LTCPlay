@echo off
rem Starts the laser listener (laser_listener.ps1, PowerShell: nothing to
rem install). BENCH TOOL: it stands where BEYOND would, listens on BEYOND's
rem OSC port and writes laser.log and laser.csv into the witness run folder.
rem It REFUSES, and starts nothing, while real BEYOND or the soak holds the
rem port. Double-click, or run with the laser_listener.ps1 options, for
rem example:  "Start laser listener.cmd" -Port 8100
rem Leave its window open; "Stop witness.cmd" stops it with the witness.
setlocal
set HERE=%~dp0
title LTC Player laser listener
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%laser_listener.ps1" %*
echo.
echo The laser listener has stopped ^(exit code %ERRORLEVEL%; 3 means it refused^).
echo Its files are laser.log and laser.csv in the witness run folder under
echo %LOCALAPPDATA%\ltcplay\witness
pause
