@echo off
REM ============================================================
REM  Capture real request templates for the three 2.1 models.
REM  Run once (or again after Doubao changes its frontend).
REM  Produces templates/*.json used by the API-first driver.
REM ============================================================
setlocal
pushd "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run start.bat first.
  popd & pause & exit /b 1
)

echo Capturing request templates for:
echo   doubao-2.1-lite / doubao-2.1-turbo / doubao-2.1-pro
echo This switches the model in the UI three times and sends a probe.
echo.
".venv\Scripts\python.exe" -m tools.capture_templates %*
set RC=%ERRORLEVEL%
popd
pause
exit /b %RC%
