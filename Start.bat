@echo off
title BioGate
cd /d "%~dp0"

echo.
echo ========================================
echo             BioGate V3
echo ========================================
echo.
echo First start creates .venv, installs dependencies and asks for an
echo administrator password. Press CTRL+C to stop.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" -OpenBrowser

pause
