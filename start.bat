@echo off
REM ============================================================
REM  Step 3 of 3: start the OpenAI-compatible server.
REM  First run creates .venv, installs deps, installs Chromium.
REM
REM  Usage:
REM    start.bat                 (default port 8787)
REM    start.bat --port 8899     (custom port)
REM ============================================================
setlocal enabledelayedexpansion
pushd "%~dp0"

set "PORT=8787"
if defined DOUBAO_PORT set "PORT=%DOUBAO_PORT%"
set "PREV="
for %%A in (%*) do (
  if "!PREV!"=="--port" set "PORT=%%A"
  set "PREV=%%A"
)

REM ---- pre-flight: is the port already taken? ----
set "BUSY="
for /f "tokens=*" %%L in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /c:":%PORT% "') do set "BUSY=%%L"
if defined BUSY (
  echo [ERROR] Port %PORT% is already in use.
  echo.
  echo   %BUSY%
  echo.
  echo   Another program is listening on that port. Either stop it,
  echo   or start this server on a different port:
  echo.
  echo       start.bat --port 8899
  echo.
  popd & pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create venv. Is Python 3.10+ on PATH?
    popd & pause & exit /b 1
  )
  echo [2/3] Installing dependencies ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed.
    popd & pause & exit /b 1
  )
  echo [3/3] Installing Chromium for Playwright ...
  ".venv\Scripts\python.exe" -m playwright install chromium
)

if not exist ".env" (
  if exist ".env.example" copy /y ".env.example" ".env" >nul
)

REM ---- warn if templates are missing (the API needs them) ----
set "TPL=0"
for %%F in (templates\doubao-2.1-*.json) do set /a TPL+=1
if %TPL%==0 (
  echo [WARN] No request templates found in templates\.
  echo        Run login.bat, then capture.bat before using the API.
  echo.
)

echo ============================================================
echo   Server starting
echo ============================================================
echo   API base : http://127.0.0.1:%PORT%/v1
echo   Models   : http://127.0.0.1:%PORT%/v1/models
echo   Health   : http://127.0.0.1:%PORT%/health
echo   Stop     : press Ctrl+C
echo ============================================================
echo.

".venv\Scripts\python.exe" -m BrowserViewer --port %PORT% %*
set RC=%ERRORLEVEL%
popd
if not "%RC%"=="0" echo [server exited with code %RC%] & pause
exit /b %RC%
