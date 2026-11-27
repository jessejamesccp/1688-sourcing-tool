@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "DIR=%~dp0"
if "%DIR:~-1%"=="\" set "DIR=%DIR:~0,-1%"
cd /d "%DIR%"
if not exist "%DIR%\data" mkdir "%DIR%\data"

set "CHROME_FOUND="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if not defined CHROME_FOUND (
  echo ==================================================
  echo [提示] 未在默认路径检测到 Google Chrome 浏览器！
  echo 请确认已安装 Chrome：https://www.google.com/chrome/
  echo ==================================================
)

set "PY_BIN="
set "PYW_BIN="
if exist "%DIR%\runtime\python.exe" (
  set "PY_BIN=%DIR%\runtime\python.exe"
  if exist "%DIR%\runtime\pythonw.exe" set "PYW_BIN=%DIR%\runtime\pythonw.exe"
) else if exist "%DIR%\.venv\Scripts\python.exe" (
  set "PY_BIN=%DIR%\.venv\Scripts\python.exe"
  if exist "%DIR%\.venv\Scripts\pythonw.exe" set "PYW_BIN=%DIR%\.venv\Scripts\pythonw.exe"
) else (
  set "SYS_PY="
  where python >nul 2>nul && set "SYS_PY=python"
  if not defined SYS_PY (
    where py >nul 2>nul && set "SYS_PY=py -3"
  )
  if not defined SYS_PY (
    echo [错误] 未检测到 Python 3，请先安装 Python 3.10+（勾选 Add Python to PATH）后重试。
    pause
    exit /b 1
  )
  echo [首次初始化] 正在创建 .venv 虚拟环境并安装依赖（请稍候）...
  %SYS_PY% -m venv "%DIR%\.venv"
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败。
    pause
    exit /b 1
  )
  set "PY_BIN=%DIR%\.venv\Scripts\python.exe"
  if exist "%DIR%\.venv\Scripts\pythonw.exe" set "PYW_BIN=%DIR%\.venv\Scripts\pythonw.exe"
  "%PY_BIN%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r "%DIR%\requirements.txt" || "%PY_BIN%" -m pip install -r "%DIR%\requirements.txt"
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接后重试。
    pause
    exit /b 1
  )
)

set "META_FILE=%DIR%\data\server.json"
set "OLD_URL="
if exist "%META_FILE%" (
  for /f "usebackq delims=" %%U in (`"%PY_BIN%" -c "import json,urllib.request; d=json.load(open(r'%META_FILE%',encoding='utf-8')); u=d.get('url',''); urllib.request.urlopen(u+'/api/config',timeout=1); print(u)" 2^>nul`) do set "OLD_URL=%%U"
)
if defined OLD_URL (
  echo 工作台已在运行: !OLD_URL!
  start "" "!OLD_URL!"
  exit /b 0
)

if exist "%META_FILE%" del /f /q "%META_FILE%" >nul 2>nul
if defined PYW_BIN (
  start "" /b "%PYW_BIN%" -u "%DIR%\web_ui.py" --no-browser >> "%DIR%\data\server.log" 2>&1
) else (
  start "" /min cmd /c ""%PY_BIN%" -u "%DIR%\web_ui.py" --no-browser >> "%DIR%\data\server.log" 2>&1"
)

set "READY_URL="
for /l %%i in (1,1,30) do (
  if exist "%META_FILE%" (
    for /f "usebackq delims=" %%U in (`"%PY_BIN%" -c "import json; print(json.load(open(r'%META_FILE%',encoding='utf-8')).get('url',''))" 2^>nul`) do set "READY_URL=%%U"
  )
  if defined READY_URL goto :launched
  timeout /t 1 /nobreak >nul
)

echo [错误] 工作台启动超时，请查看日志: %DIR%\data\server.log
pause
exit /b 1

:launched
echo ==================================================
echo 1688 选品工作台已启动: %READY_URL%
echo 如需停止后台服务，请双击「停止工作台.bat」或点击页面右上角「停止服务」。
echo ==================================================
start "" "%READY_URL%"
exit /b 0
