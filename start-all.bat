@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"
set "RUN_DIR=%ROOT%.run"
if not exist "%RUN_DIR%" mkdir "%RUN_DIR%"

if not exist "%ROOT%config.yaml" (
  echo [WARN] config.yaml not found. Please create it before starting the backend.
)

set "PYTHON_CMD="
if exist "%ROOT%backend\.venv\Scripts\python.exe" set "PYTHON_CMD=%ROOT%backend\.venv\Scripts\python.exe"
if not defined PYTHON_CMD where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  where py >nul 2>nul && set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  echo [ERROR] Python was not found in PATH.
  pause
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found in PATH.
  pause
  exit /b 1
)

set "START_ROOT=%ROOT%"

if not exist "%ROOT%backend\.venv\Scripts\python.exe" (
  echo [SETUP] creating backend virtualenv
  %PYTHON_CMD% -m venv "%ROOT%backend\.venv"
)
set "PYTHON_CMD=%ROOT%backend\.venv\Scripts\python.exe"

echo [SETUP] checking backend Python dependencies
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=$env:START_ROOT; $py=Join-Path $root 'backend\.venv\Scripts\python.exe'; $req=Join-Path $root 'backend\requirements.txt'; $stamp=Join-Path $root '.run\backend.requirements.sha256'; $hash=(Get-FileHash $req -Algorithm SHA256).Hash; $needs=(!(Test-Path $stamp)) -or ((Get-Content $stamp -ErrorAction SilentlyContinue) -ne $hash); if (-not $needs) { & $py -c 'import fastapi, uvicorn, httpx, yaml, requests, PIL, lz4' 2>$null; $needs=($LASTEXITCODE -ne 0) }; if ($needs) { & $py -m pip install --upgrade pip setuptools wheel; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; & $py -m pip install -r $req; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; Set-Content -Path $stamp -Value $hash -Encoding ASCII }"
if errorlevel 1 (
  echo [ERROR] Failed to install backend Python dependencies.
  pause
  exit /b 1
)

for /f %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=3000; while ($p -le 3999 -and (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)) { $p++ }; if ($p -gt 3999) { exit 1 }; $p"') do set "FRONTEND_PORT=%%P"
if not defined FRONTEND_PORT (
  echo [ERROR] No free frontend port found in 3000-3999.
  pause
  exit /b 1
)

echo Starting backend...
set "START_PYTHON_CMD=%PYTHON_CMD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd='title WeChat Web Backend && cd /d \"' + $env:START_ROOT + 'backend\" && ' + $env:START_PYTHON_CMD + ' main.py'; $p=Start-Process -FilePath cmd.exe -ArgumentList @('/k', $cmd) -PassThru; Set-Content -Path ($env:START_ROOT + '.run\backend.pid') -Value $p.Id -Encoding ASCII"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2"

echo Starting frontend on port %FRONTEND_PORT%...
set "START_FRONTEND_PORT=%FRONTEND_PORT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd='title WeChat Web Frontend && cd /d \"' + $env:START_ROOT + 'frontend\" && set FRONTEND_PORT=' + $env:START_FRONTEND_PORT + '&& if not exist node_modules (npm install && npm run dev) else (npm run dev)'; $p=Start-Process -FilePath cmd.exe -ArgumentList @('/k', $cmd) -PassThru; Set-Content -Path ($env:START_ROOT + '.run\frontend.pid') -Value $p.Id -Encoding ASCII; Set-Content -Path ($env:START_ROOT + '.run\frontend.port') -Value $env:START_FRONTEND_PORT -Encoding ASCII"

echo.
echo Backend and frontend startup commands were launched.
echo Frontend: http://127.0.0.1:%FRONTEND_PORT%
echo Backend:  http://127.0.0.1:5000

endlocal
