@echo off
rem Double-click to download the latest bot code. Stop the bot first.
cd /d "%~dp0"
git pull --ff-only || goto :error
if exist .venv\Scripts\python.exe .venv\Scripts\python -m pip install -q -r requirements.txt
echo.
echo Updated. Start the bot again with start.bat
goto :end
:error
echo Update failed. Is Git installed, and did you start the bot from this folder?
:end
pause
