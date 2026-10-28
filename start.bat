@echo off
rem Double-click to start polybot. Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
    echo First run: installing...
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install -q -r requirements.txt || goto :error
)
if not exist .env (
    copy .env.example .env >nul
    echo Created .env - fill it in, then run start.bat again.
    notepad .env
    goto :end
)
rem Open the dashboard once the bot has had a few seconds to start.
start "" /min cmd /c "timeout /t 10 >nul & start http://localhost:8080"
.venv\Scripts\python -m polybot.main
goto :end
:error
echo Setup failed. Is Python installed and on PATH?
:end
pause
