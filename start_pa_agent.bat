@echo off
REM ============================================================
REM  PA Agent Launcher
REM  Project : Price Action AI Analysis Agent
REM  Usage   : Double-click this file to start the GUI.
REM  Location: this project directory
REM ============================================================
title PA Agent

cd /d "%~dp0"
echo ============================================================
echo  Starting PA Agent (Price Action AI Analysis)...
echo  Project dir: %CD%
echo ============================================================
echo.

REM Try python in PATH first; fall back to the managed runtime path.
where python >nul 2>nul
if %errorlevel%==0 (
    python run.py
) else (
    py -3 run.py
)

echo.
echo ============================================================
echo  PA Agent has exited.
echo  If the window closed unexpectedly, check:
echo    %CD%\logs\pa_agent.log
echo    %CD%\logs\crash.log
echo ============================================================
pause
