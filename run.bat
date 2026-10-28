@echo off
rem Double-click, type how many minutes to run (or just press Enter to run until you stop it).
rem Stop early any time from your phone with /stop, or change the time with /stopin <minutes>.
cd /d "%~dp0"
set RUN_FOR_MINUTES=
set /p RUN_FOR_MINUTES=Run for how many minutes? (Enter = until I stop it): 
if "%RUN_FOR_MINUTES%"=="" set RUN_FOR_MINUTES=0
call start.bat
