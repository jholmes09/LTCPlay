@echo off
rem Stops the running wire witness by putting a STOP file in its run folder
rem (the witness checks for it every second and closes its files cleanly).
setlocal
set CUR=%LOCALAPPDATA%\ltcplay\witness\witness-current.txt
if not exist "%CUR%" (
    echo No witness run is on record ^(%CUR% is missing^). Nothing was changed.
    pause
    exit /b 1
)
set /p RUN=<"%CUR%"
echo stop> "%RUN%\STOP"
echo Asked the witness in %RUN% to stop.
pause
