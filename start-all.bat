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
if defined PYTHON_BIN set "PYTHON_CMD=%PYTHON_BIN%"
if not defined PYTHON_CMD (
  where py >nul 2>nul && py -3.13 -c "import sys" >nul 2>nul && set "PYTHON_CMD=py -3.13"
)
if not defined PYTHON_CMD where python3.13 >nul 2>nul && set "PYTHON_CMD=python3.13"
if not defined PYTHON_CMD where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  echo [ERROR] Python was not found in PATH.
  pause
  exit /b 1
)

%PYTHON_CMD% -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 13))" >nul 2>nul
if errorlevel 1 (
  echo [WARN] Python 3.13 was not found. Set PYTHON_BIN to a Python 3.13 executable to run the backend on Python 3.13.
)

where npm >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found in PATH.
  pause
  exit /b 1
)

set "START_ROOT=%ROOT%"
set "VENV_PY=%ROOT%backend\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" > "%RUN_DIR%\backend.venv.version.tmp" 2>nul
  if errorlevel 1 (
    echo [SETUP] removing broken backend virtualenv
    rmdir /s /q "%ROOT%backend\.venv"
  ) else (
    %PYTHON_CMD% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" > "%RUN_DIR%\backend.python.version.tmp" 2>nul
    fc "%RUN_DIR%\backend.venv.version.tmp" "%RUN_DIR%\backend.python.version.tmp" >nul 2>nul
    if errorlevel 1 (
      echo [SETUP] recreating backend virtualenv for selected Python
      rmdir /s /q "%ROOT%backend\.venv"
    )
  )
  del /f /q "%RUN_DIR%\backend.venv.version.tmp" "%RUN_DIR%\backend.python.version.tmp" >nul 2>nul
)

if not exist "%VENV_PY%" (
  echo [SETUP] creating backend virtualenv
  %PYTHON_CMD% -m venv "%ROOT%backend\.venv"
)
set "PYTHON_CMD=%VENV_PY%"

echo [SETUP] checking backend Python dependencies
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=$env:START_ROOT; $py=Join-Path $root 'backend\.venv\Scripts\python.exe'; $req=Join-Path $root 'backend\requirements.txt'; $stamp=Join-Path $root '.run\backend.requirements.sha256'; $sha=[System.Security.Cryptography.SHA256]::Create(); $hash=([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($req))).Replace('-','')).ToUpperInvariant(); $needs=(!(Test-Path $stamp)) -or ((Get-Content $stamp -ErrorAction SilentlyContinue) -ne $hash); if (-not $needs) { & $py -c 'import fastapi, uvicorn, httpx, yaml, requests, PIL, lz4' 2>$null; $needs=($LASTEXITCODE -ne 0) }; if ($needs) { & $py -m pip install --upgrade pip setuptools wheel; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; & $py -m pip install -r $req; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; Set-Content -Path $stamp -Value $hash -Encoding ASCII }"
if errorlevel 1 (
  echo [ERROR] Failed to install backend Python dependencies.
  pause
  exit /b 1
)

echo Starting backend...
set "START_PYTHON_CMD=%PYTHON_CMD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd='title WeChat Web Backend && cd /d \"' + $env:START_ROOT + 'backend\" && ' + $env:START_PYTHON_CMD + ' main.py'; $p=Start-Process -FilePath cmd.exe -ArgumentList @('/k', $cmd) -PassThru; Set-Content -Path ($env:START_ROOT + '.run\backend.pid') -Value $p.Id -Encoding ASCII"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2"

echo Starting frontend...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd='title WeChat Web Frontend && cd /d \"' + $env:START_ROOT + 'frontend\" && if not exist node_modules (npm install && npm run dev) else (npm run dev)'; $p=Start-Process -FilePath cmd.exe -ArgumentList @('/k', $cmd) -PassThru; Set-Content -Path ($env:START_ROOT + '.run\frontend.pid') -Value $p.Id -Encoding ASCII"

echo.
echo Backend and frontend startup commands were launched.
echo Frontend and backend host/port are configured by config.yaml.

endlocal
