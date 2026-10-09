@echo off
setlocal
set DIR=%~dp0
where node >nul 2>nul
if errorlevel 1 (
  echo [error] node.exe not found. Install Node.js 22+ or set MIMO_USAGE_NODE.
  pause
  exit /b 1
)
echo Starting MiMo usage dashboard... close this window to stop the server.
node "%DIR%server.mjs" --port 8790 --open
