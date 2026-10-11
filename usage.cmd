@echo off
REM ============================================================================
REM  MiMo Desktop 用量看板 —— 一键生成并打开（不需要 MiMo、不需要 AI）
REM
REM  双击本文件即可：生成静态 HTML 并用默认浏览器打开。
REM  也可以带参数，例如：  usage.cmd token --days 7
REM ============================================================================
setlocal

set "SCRIPT=%~dp0usage_cli.py"
set "PY="

REM 依次尝试常见 Python 位置
for %%P in (python.exe) do (
  if not defined PY (
    for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined PY set "PY=%%I"
  )
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"

if not defined PY (
  echo [usage] Python not found. Install Python 3.10+ or set MIMO_USAGE_PY.
  pause
  exit /b 2
)

"%PY%" "%SCRIPT%" %* --open
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
endlocal & exit /b %RC%
