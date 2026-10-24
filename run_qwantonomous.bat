@echo off
REM ===========================================================================
REM  Qwantonomous / OpenAlgo launcher
REM
REM  Runs app.py from THIS directory using its OWN virtualenv.
REM
REM  The previous version did `cd /d "C:\opt\servers\qwantonomous"`, which is
REM  now an EMPTY directory - so the launcher could never start this tree (and
REM  silently ran nothing, or whatever happened to sit at that path). %~dp0 is
REM  the folder holding THIS file, so the launcher always follows the checkout
REM  it lives in and cannot go stale again.
REM ===========================================================================
setlocal

cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo.
    echo ERROR: no virtualenv found at "%~dp0.venv"
    echo.
    echo Create it once with:
    echo     cd /d "%~dp0"
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo  Starting OpenAlgo from "%~dp0"
echo  Interpreter: %PYTHON%
echo ============================================================
echo.

"%PYTHON%" app.py
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo app.py exited with code %EXITCODE%
    pause
)

exit /b %EXITCODE%
