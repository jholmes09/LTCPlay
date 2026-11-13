@echo off
rem Make me SuperUser (SUPERUSER.md). Double-click it on the
rem show PC, in the LTC Player folder. It asks for each name and password
rem in this window; passwords are hidden as they are typed. Nobody types a
rem command. It runs: ltcplay.exe superuser NAME
setlocal
cd /d "%~dp0"
title Make me SuperUser
echo Make me SuperUser
echo  - Sign in once on the rack screen first.
echo  - Passwords stay hidden as you type.
echo.
set "NAME="
set /p "NAME=Your name in LTC Player: "
if not "%NAME%"=="" "%~dp0ltcplay.exe" superuser "%NAME%"
if "%NAME%"=="" echo Nothing was changed.
echo.
pause
