@echo off
rem Starts the wire witness (witness.ps1, PowerShell: nothing to install).
rem BENCH TOOL: it only listens and writes files. Double-click, or run with
rem the witness.ps1 options, for example:  "Start witness.cmd" -Companion
rem Leave its window open; close it with Ctrl-C or with "Stop witness.cmd".
setlocal
set HERE=%~dp0
title LTC Player wire witness
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%witness.ps1" %*
echo.
echo The witness has stopped. Its files are under %LOCALAPPDATA%\ltcplay\witness
pause
