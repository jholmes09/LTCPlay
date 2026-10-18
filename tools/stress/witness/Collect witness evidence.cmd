@echo off
rem Zips everything the checker and the soak summary need onto the Desktop,
rem for a machine that has Python (Jeff's Mac, or the cloud session). No show
rem files, media or keys; the soak's generated shows are left out. Nothing
rem is changed.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0collect_evidence.ps1"
pause
