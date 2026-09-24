@echo off
REM ============================================================
REM  Step 1 of 3: login + human verification + self check.
REM  Opens a browser window. Scan the QR code to log in.
REM  If a captcha appears, solve it manually in that window.
REM  Run this first, or whenever the session expires.
REM ============================================================
setlocal
pushd "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run start.bat first.
  popd & pause & exit /b 1
)

echo ============================================================
echo   Login / verification / self check
echo ============================================================
echo   A browser window will open. Log in by scanning the QR code.
echo   If a captcha shows up, solve it manually in that window.
echo   This window waits and continues automatically.
echo ============================================================
echo.

".venv\Scripts\python.exe" -m tools.login %*
set RC=%ERRORLEVEL%
popd
pause
exit /b %RC%
