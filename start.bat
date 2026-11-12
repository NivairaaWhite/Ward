@echo off
setlocal
REM start.bat -- double-click this. It calls the venv's python.exe directly,
REM so no PowerShell activation or execution-policy prompts are involved.
REM Dependencies are only (re)installed when requirements.txt changes, so Ward
REM also starts fine offline after the first run.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ from python.org
    echo ^(check "Add python.exe to PATH" during install^), then run this again.
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Setting up Ward for the first time -- this only happens once...
    python -m venv venv
    if errorlevel 1 (
        echo Could not create a virtual environment. Is Python installed correctly?
        pause
        exit /b 1
    )
)

fc /b requirements.txt "venv\.installed-requirements.txt" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    "venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed -- see the error above.
        pause
        exit /b 1
    )
    copy /y requirements.txt "venv\.installed-requirements.txt" >nul
)

if not exist ".env" (
    echo No .env found -- copying .env.example so Ward has one.
    echo Ward runs fine without editing it -- reverse-image search just
    echo stays off until you add a free SerpApi key under Settings.
    copy /y ".env.example" ".env" >nul
)

echo.
echo Starting Ward -- it will open in your browser.
echo Close this window to stop Ward.
echo.
"venv\Scripts\python.exe" desktop.py
pause
