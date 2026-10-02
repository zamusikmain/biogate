@echo off
title BioGate
cd /d "%~dp0"

echo.
echo ========================================
echo             BioGate V3
echo ========================================
echo.
echo Login:
echo http://127.0.0.1:8000/
echo.
echo Admin:
echo http://127.0.0.1:8000/admin
echo.
echo Starting BioGate...
echo Press CTRL+C to stop.
echo ========================================
echo.

start "" /b cmd /c "timeout /t 5 /nobreak >nul & start "" http://127.0.0.1:8000/"

.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload

pause
