@echo off
rem Reset SuperUser sign-in (SUPERUSER.md). Double-click it
rem on the show PC, in the LTC Player folder. A SuperUser's name and
rem password confirm it; the new password is temporary, and its owner
rem chooses their own at the next sign-in on the rack screen. Passwords are
rem hidden as they are typed. It runs: ltcplay.exe superuser-password NAME
setlocal
cd /d "%~dp0"
title Reset SuperUser sign-in
echo Reset SuperUser sign-in
echo  - A SuperUser's password confirms it.
echo  - The new one is temporary.
echo.
set "NAME="
set /p "NAME=Whose password (their name): "
if not "%NAME%"=="" "%~dp0ltcplay.exe" superuser-password "%NAME%"
if "%NAME%"=="" echo Nothing was changed.
echo.
pause
