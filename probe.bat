@echo off
REM ============================================================
REM  Probe: verify in-page fetch + signing works, and dump the
REM  raw SSE frames for troubleshooting.
REM ============================================================
setlocal
pushd "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run start.bat first.
  popd & pause & exit /b 1
)

".venv\Scripts\python.exe" -m tools.probe_api %*
set RC=%ERRORLEVEL%
popd
pause
exit /b %RC%
