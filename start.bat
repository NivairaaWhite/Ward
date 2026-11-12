@echo off
setlocal
REM start.bat -- double-click this. No PowerShell, no "activating" a venv, no
REM execution-policy prompts: this calls the venv's python.exe directly by
REM path the whole way through, which is legal under Windows' default
REM execution policy (Restricted only blocks running *.ps1 scripts like
REM venv\Scripts\Activate.ps1 -- it says nothing about running an .exe).
REM If you hit "cannot be loaded because running scripts is disabled on this
REM system" before, that was Activate.ps1 specifically; this script never
REM calls it.

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

REM Only touch the network when requirements.txt changed since the last
REM successful install, so Ward starts offline after the first run.
REM We compare against a saved copy of requirements.txt with `fc` instead of
REM hashing it with a python one-liner inside `for /f`: cmd strips the outer
REM quotes of a quoted command in `for /f`, which mangled the old one-liner,
REM left the hash empty, and made this script think nothing needed installing.
set NEED_INSTALL=1
if exist "venv\.requirements.installed" (
    fc /b "requirements.txt" "venv\.requirements.installed" >nul 2>nul
    if not errorlevel 1 set NEED_INSTALL=0
)
if "%NEED_INSTALL%"=="1" (
    echo Installing dependencies -- the first time this takes a few minutes...
    "venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    "venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency install failed -- see the error above.
        pause
        exit /b 1
    )
    copy /y "requirements.txt" "venv\.requirements.installed" >nul
)

if not exist ".env" (
    echo No .env found -- copying .env.example so Ward has one.
    echo Ward runs fine without editing it -- reverse-image search just
    echo stays off until you add a free SerpApi key under Settings.
    copy /y ".env.example" ".env" >nul
)

echo.
echo Starting Ward -- it opens in your browser...
echo Close this window to stop Ward.
echo.
"venv\Scripts\python.exe" desktop.py
pause
