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

call :ensure_node
if errorlevel 1 (
  echo [ERROR] Failed to install Node.js LTS and npm.
  echo Please check your network connection, approve the Windows installer prompt, and try again.
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
exit /b 0

:ensure_node
call :refresh_path
where node >nul 2>nul
if errorlevel 1 goto install_node
where npm.cmd >nul 2>nul
if not errorlevel 1 exit /b 0

:install_node
echo [SETUP] npm was not found. Installing Node.js LTS ^(includes npm^)...
where winget >nul 2>nul
if errorlevel 1 goto install_node_from_web

winget install --id OpenJS.NodeJS.LTS --exact --source winget --accept-package-agreements --accept-source-agreements --silent --force
if not errorlevel 1 goto verify_node
echo [WARN] winget could not install Node.js. Trying the official Node.js installer...

:install_node_from_web
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $releases=Invoke-RestMethod 'https://nodejs.org/dist/index.json'; $release=$releases | Where-Object { $_.lts } | Select-Object -First 1; if (-not $release) { throw 'Could not find the current Node.js LTS release.' }; $preferredArch=if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq [Runtime.InteropServices.Architecture]::Arm64) { 'arm64' } elseif ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }; $arch=if ($release.files -contains ('win-' + $preferredArch + '-msi')) { $preferredArch } elseif ($release.files -contains 'win-x64-msi') { 'x64' } else { 'x86' }; $msi=Join-Path $env:TEMP ('node-' + [Guid]::NewGuid().ToString('N') + '.msi'); try { $url='https://nodejs.org/dist/' + $release.version + '/node-' + $release.version + '-' + $arch + '.msi'; Write-Host ('[SETUP] Downloading ' + $url); Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $msi; $signature=Get-AuthenticodeSignature -FilePath $msi; if ($signature.Status -ne 'Valid') { throw ('Invalid installer signature: ' + $signature.Status) }; $process=Start-Process -FilePath 'msiexec.exe' -Verb RunAs -ArgumentList @('/i', ('"' + $msi + '"'), '/passive', '/norestart') -Wait -PassThru; if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) { throw ('Node.js installer exited with code ' + $process.ExitCode) } } finally { Remove-Item -LiteralPath $msi -Force -ErrorAction SilentlyContinue }"
if errorlevel 1 exit /b 1

:verify_node
call :refresh_path
where node >nul 2>nul
if errorlevel 1 exit /b 1
where npm.cmd >nul 2>nul
if errorlevel 1 exit /b 1
for /f "delims=" %%V in ('node --version') do echo [SETUP] Node.js %%V is ready.
for /f "delims=" %%V in ('npm.cmd --version') do echo [SETUP] npm %%V is ready.
exit /b 0

:refresh_path
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%PATH%;%%P"
if exist "%ProgramFiles%\nodejs\npm.cmd" set "PATH=%ProgramFiles%\nodejs;%PATH%"
if exist "%LocalAppData%\Programs\nodejs\npm.cmd" set "PATH=%LocalAppData%\Programs\nodejs;%PATH%"
exit /b 0
