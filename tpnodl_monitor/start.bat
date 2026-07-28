@echo off
title TPNODL Monitor Launcher
cd /d "%~dp0"

echo Checking dependencies...
python -c "import pystray" 2>nul || (
    echo Installing pystray...
    pip install pystray pillow -q
)

echo Stopping previous instance...
python kill_prev.py

timeout /t 2 /nobreak >nul

echo Finding free port...
for /f %%P in ('python find_free_port.py') do set TPNODL_PORT=%%P

if "%TPNODL_PORT%"=="" (
    echo ERROR: Could not find free port. Exiting.
    pause
    exit /b 1
)

echo Using port: %TPNODL_PORT%
echo Starting TPNODL Monitor in system tray...
start "" pythonw tray.pyw

timeout /t 4 /nobreak >nul
echo.
echo ============================================
echo  TPNODL Monitor started successfully!
echo  Dashboard: http://127.0.0.1:%TPNODL_PORT%
echo  Network:   http://10.40.107.137:%TPNODL_PORT%
echo  Check system tray (bottom-right corner)
echo ============================================
echo.
timeout /t 3 /nobreak >nul
