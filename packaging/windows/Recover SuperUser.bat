@echo off
rem Recover SuperUser (SUPERUSER.md). Double-click it on the show PC, in
rem the LTC Player folder, when a SuperUser forgot their password. It asks
rem for the recovery code written down when the first SuperUser was made
rem (hidden as it is typed), then a new temporary password, and shows the
rem NEW recovery code once. It runs: ltcplay.exe superuser-recover NAME
setlocal
cd /d "%~dp0"
title Recover SuperUser
echo Recover SuperUser
echo  - Have your recovery code ready.
echo  - Write down the new code shown.
echo.
set "NAME="
set /p "NAME=SuperUser's name: "
if not "%NAME%"=="" "%~dp0ltcplay.exe" superuser-recover "%NAME%"
if "%NAME%"=="" echo Nothing was changed.
echo.
pause
