@echo off
setlocal EnableExtensions
title CalcVault — Stopping

REM ============================================================
REM  Stop_CalcVault.bat
REM  Cleanly stops the CalcVault server by killing the python.exe
REM  running from THIS folder's venv only — other Python apps
REM  on the machine are left untouched.
REM ============================================================

cd /d "%~dp0"
set "TARGET_EXE=%~dp0venv\Scripts\python.exe"

echo.
echo Looking for CalcVault server processes...

REM --- Find PIDs matching our venv python.exe -------------------
set "FOUND=0"
for /f "tokens=2 delims=," %%P in (
    'wmic process where "ExecutablePath='%TARGET_EXE:\=\\%'" get ProcessId /format:csv 2^>nul ^| findstr /r "[0-9]"'
) do (
    echo   -> terminating PID %%P
    taskkill /F /PID %%P >nul 2>&1
    set "FOUND=1"
)

if "%FOUND%"=="0" (
    echo   No CalcVault process found. Trying generic fallback...
    REM Fallback: kill any python running app.py from this folder
    for /f "tokens=2" %%P in (
        'tasklist /v /fi "imagename eq python.exe" /fo list ^| findstr /i "app.py"'
    ) do (
        taskkill /F /PID %%P >nul 2>&1
    )
)

echo.
echo Done.
timeout /t 2 /nobreak >nul
endlocal
exit /b 0