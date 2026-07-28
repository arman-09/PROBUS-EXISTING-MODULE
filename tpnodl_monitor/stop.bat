@echo off
title TPNODL Monitor - Stop
cd /d "%~dp0"
echo Stopping TPNODL Monitor...

:: Kill by PID file (no admin needed - kills own processes)
python kill_prev.py

:: Also kill by port if port file exists
if exist "data\.port" (
    for /f %%P in (data\.port) do (
        echo Freeing port %%P...
        for /f "tokens=5" %%X in ('netstat -ano 2^>nul ^| findstr " :%%P " ^| findstr "LISTENING"') do (
            taskkill /F /PID %%X >nul 2>&1
        )
    )
    del "data\.port" >nul 2>&1
)

echo Done.
timeout /t 2 /nobreak >nul
