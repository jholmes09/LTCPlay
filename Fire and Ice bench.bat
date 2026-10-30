@echo off
rem Fire & Ice bench on the Pico. See BENCH.md.
rem This window IS the engine. Closing it stops everything.
cd /d "%~dp0"
py -m ltcplay.cli serve --schedule
pause
