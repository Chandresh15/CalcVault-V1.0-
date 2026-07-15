@echo off
setlocal EnableExtensions EnableDelayedExpansion
title CalcVault — Ramboll Edition

REM ============================================================
REM  Start_CalcVault.bat
REM  Bootstraps venv (first run), installs deps, launches app,
REM  opens the default browser to http://127.0.0.1:5000
REM ============================================================

REM --- Always work from the script's own directory --------------
cd /d "%~dp0"

REM --- Config ---------------------------------------------------
set "APP_DIR=%~dp0"
set "VENV_DIR=%APP_DIR%venv"
set "PY_EXE=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%APP_DIR%requirements.txt"
set "REQ_MARK=%VENV_DIR%\.requirements_installed"
set "PORT=5000"

echo.
echo ============================================================
echo   Ramboll CalcVault  --  local intranet launcher
echo ============================================================
echo   Folder : %APP_DIR%
echo   Port   : %PORT%
echo ============================================================
echo.

REM --- 1. Locate Python (system) --------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    echo         Install Python 3.11+ from https://python.org/downloads
    echo         and tick "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM --- 2. Create venv on first run ------------------------------
if not exist "%PY_EXE%" (
    echo [1/4] Creating virtual environment ^(one-time^)...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
    REM Force pip to install requirements after a fresh venv
    if exist "%REQ_MARK%" del /q "%REQ_MARK%"
) else (
    echo [1/4] Virtual environment already present. Skipping create.
)

REM --- 3. Install / refresh dependencies ------------------------
REM   Re-run pip only if requirements.txt is newer than our marker.
set "NEED_INSTALL=0"
if not exist "%REQ_MARK%"  set "NEED_INSTALL=1"
if exist "%REQ_MARK%" (
    for %%A in ("%REQ_FILE%") do set "REQ_TIME=%%~tA"
    for %%A in ("%REQ_MARK%") do set "MARK_TIME=%%~tA"
    if not "!REQ_TIME!"=="!MARK_TIME!" set "NEED_INSTALL=1"
)

if "%NEED_INSTALL%"=="1" (
    echo [2/4] Installing dependencies from requirements.txt...
    "%PY_EXE%" -m pip install --upgrade pip >nul
    "%PY_EXE%" -m pip install -r "%REQ_FILE%"
    if errorlevel 1 (
        echo.
        echo [ERROR] pip install failed. See messages above.
        pause
        exit /b 1
    )
    REM Touch marker to mirror requirements.txt timestamp
    copy /y /b "%REQ_FILE%" +,, "%REQ_MARK%" >nul
    echo         Dependencies installed.
) else (
    echo [2/4] Dependencies already up to date.
)

REM --- 4. Make sure runtime folders exist -----------------------
echo [3/4] Preparing runtime folders...
if not exist "%APP_DIR%uploads"           mkdir "%APP_DIR%uploads"
if not exist "%APP_DIR%uploads\pump_pdfs" mkdir "%APP_DIR%uploads\pump_pdfs"

REM --- 5. Open browser after a short delay ----------------------
echo [4/4] Launching server + opening browser...
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:%PORT%"

echo.
echo ============================================================
echo   Server console below.  Press Ctrl+C to stop, or run
echo   Stop_CalcVault.bat from another window.
echo ============================================================
echo.

REM --- 6. Run the app (blocks until Ctrl+C or Shutdown button) --
set "CV_PORT=%PORT%"
"%PY_EXE%" "%APP_DIR%app.py" --no-browser

REM --- 7. Cleanup ------------------------------------------------
echo.
echo Server stopped.
pause
endlocal
exit /b 0