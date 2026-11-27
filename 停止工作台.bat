@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
cd /d "%DIR%"

set "PY_BIN="
if exist "%DIR%\runtime\python.exe" (
  set "PY_BIN=%DIR%\runtime\python.exe"
) else if exist "%DIR%\.venv\Scripts\python.exe" (
  set "PY_BIN=%DIR%\.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul && set "PY_BIN=python"
)

set "META_FILE=%DIR%\data\server.json"
set "SERVER_PID="
set "SERVER_URL="

if exist "%META_FILE%" if defined PY_BIN (
  for /f "usebackq tokens=1,2 delims=|" %%A in (`"%PY_BIN%" -c "import json; d=json.load(open(r'%META_FILE%',encoding='utf-8')); print(f\"{int(d.get('pid') or 0)}|{d.get('url','')}\")" 2^>nul`) do (
    set "SERVER_PID=%%A"
    set "SERVER_URL=%%B"
  )
)

if defined SERVER_URL (
  powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri '%SERVER_URL%/api/shutdown' -ContentType 'application/json' -Body '{}' -TimeoutSec 2 | Out-Null } catch {}" >nul 2>nul
  timeout /t 1 /nobreak >nul
)

if defined SERVER_PID (
  if not "%SERVER_PID%"=="0" (
    taskkill /PID %SERVER_PID% /T /F >nul 2>nul
  )
)

if exist "%META_FILE%" del /f /q "%META_FILE%" >nul 2>nul
echo 1688 选品工作台及关联进程已停止。
timeout /t 2 >nul
exit /b 0
