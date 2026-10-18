@echo off
rem Runs the checker on the current witness run, with the engine's machine
rem log and flamesafe's journal from Settings and logs. Needs Python on this
rem PC; without it, use "Collect witness evidence.cmd" and run the checker
rem where Python is (Jeff's Mac, the cloud session).
setlocal
set HERE=%~dp0
set AD=%LOCALAPPDATA%\ltcplay
set CUR=%AD%\witness\witness-current.txt
if not exist "%CUR%" (
    echo No witness run is on record ^(%CUR% is missing^).
    pause
    exit /b 1
)
set /p RUN=<"%CUR%"
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY where python >nul 2>&1 && set PY=python
if not defined PY (
    echo This PC has no Python, so the checker cannot run here.
    echo Double-click "Collect witness evidence.cmd" and send the zip from the
    echo Desktop to the session that runs check_witness.py.
    pause
    exit /b 2
)
%PY% "%HERE%check_witness.py" --witness "%RUN%" --nights "%AD%\nights" --flamesafe-log "%AD%\logs\flamesafe.log" %*
echo.
echo Exit code %ERRORLEVEL% ^(0 PASS, 1 FAIL^). The report is check_report.txt in %RUN%
pause
