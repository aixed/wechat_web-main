@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "LEGACY_NODE_PLATFORM=0"
ver | %SystemRoot%\System32\findstr.exe /r /c:"6\.[0-3]\." >nul
if not errorlevel 1 set "LEGACY_NODE_PLATFORM=1"
if "%LEGACY_NODE_PLATFORM%"=="1" (
  set "NODE_SKIP_PLATFORM_CHECK=1"
  echo [WARN] Legacy Windows detected. Enabling NODE_SKIP_PLATFORM_CHECK for Node.js.
  echo [WARN] Windows Server 2012 R2 is not officially supported by current Node.js releases.
)

set "ROOT=%~dp0"
set "RUN_DIR=%ROOT%.run"
set "START_ROOT=%ROOT%"
if not exist "%RUN_DIR%" mkdir "%RUN_DIR%"

if not exist "%ROOT%config.yaml" (
  if exist "%ROOT%config.example.yaml" (
    echo [SETUP] config.yaml not found. Creating it from config.example.yaml.
    copy /y "%ROOT%config.example.yaml" "%ROOT%config.yaml" >nul
  ) else (
    echo [ERROR] config.yaml and config.example.yaml were not found.
    pause
    exit /b 1
  )
)

call :ensure_web_access_key
if errorlevel 1 (
  pause
  exit /b 1
)

call :cleanup_stale_processes
if errorlevel 1 (
  pause
  exit /b 1
)

call :ensure_python
if errorlevel 1 (
  echo [ERROR] Failed to install Python 3.13.
  echo Please check your network connection and try again.
  pause
  exit /b 1
)

call :ensure_node
if errorlevel 1 (
  echo [ERROR] Failed to install Node.js LTS and npm.
  echo Please check your network connection, approve the Windows installer prompt, and try again.
  pause
  exit /b 1
)

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
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=$env:START_ROOT; $py=Join-Path $root 'backend\.venv\Scripts\python.exe'; $req=Join-Path $root 'backend\requirements.txt'; $stamp=Join-Path $root '.run\backend.requirements.sha256'; $sha=[System.Security.Cryptography.SHA256]::Create(); $hash=([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($req))).Replace('-','')).ToUpperInvariant(); $needs=(!(Test-Path $stamp)) -or ((Get-Content $stamp -ErrorAction SilentlyContinue) -ne $hash); if (-not $needs) { & $py -c 'import fastapi, uvicorn, httpx, yaml, ruamel.yaml, requests, PIL, lz4' 2>$null; $needs=($LASTEXITCODE -ne 0) }; if ($needs) { & $py -m pip install --upgrade pip setuptools wheel; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; & $py -m pip install -r $req; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; Set-Content -Path $stamp -Value $hash -Encoding ASCII }"
if errorlevel 1 (
  echo [ERROR] Failed to install backend Python dependencies.
  pause
  exit /b 1
)

call :select_frontend_port
if errorlevel 1 (
  pause
  exit /b 1
)

echo Starting backend...
set "START_PYTHON_CMD=%PYTHON_CMD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd='title WeChat Web Backend && cd /d \"' + $env:START_ROOT + 'backend\" && ' + $env:START_PYTHON_CMD + ' main.py'; $p=Start-Process -FilePath cmd.exe -ArgumentList @('/k', $cmd) -PassThru; Set-Content -Path ($env:START_ROOT + '.run\backend.pid') -Value $p.Id -Encoding ASCII"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2"

echo Starting frontend...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd='title WeChat Web Frontend && call \"' + $env:START_ROOT + 'scripts\start-frontend-dev.bat\" \"' + $env:START_ROOT + 'frontend\"'; $p=Start-Process -FilePath cmd.exe -ArgumentList @('/k', $cmd) -PassThru; Set-Content -Path ($env:START_ROOT + '.run\frontend.pid') -Value $p.Id -Encoding ASCII"

set "FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%"
set "START_FRONTEND_URL=%FRONTEND_URL%"
echo [SETUP] Waiting for %FRONTEND_URL% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$deadline=[DateTime]::UtcNow.AddSeconds(180); while ([DateTime]::UtcNow -lt $deadline) { try { $response=Invoke-WebRequest -UseBasicParsing -Uri $env:START_FRONTEND_URL -TimeoutSec 2; if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { exit 0 } } catch {}; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
  echo [WARN] Frontend did not respond within 180 seconds. Browser was not opened.
) else (
  echo [READY] Frontend is available at %FRONTEND_URL%
  start "" "%FRONTEND_URL%"
)

echo.
echo Backend and frontend startup commands were launched.
echo Frontend port %FRONTEND_PORT% was written to config.yaml.

set "START_ALL_CLOSE_CMD=0"
set "START_ALL_CMDLINE=%CMDCMDLINE%"
if /i not "%START_ALL_CMDLINE:start-all.bat=%"=="%START_ALL_CMDLINE%" set "START_ALL_CLOSE_CMD=1"
if "%START_ALL_CLOSE_CMD%"=="1" (endlocal & exit 0) else (endlocal & exit /b 0)

:ensure_web_access_key
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\ensure-web-access-key.ps1" -ConfigPath "%ROOT%config.yaml" -DefaultKey "admin"
if errorlevel 1 (
  echo [ERROR] Failed to configure web_access_key in config.yaml.
  exit /b 1
)
exit /b 0

:cleanup_stale_processes
echo [SETUP] Stopping stale backend/frontend processes...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\stop-project-processes.ps1" -RootPath "%ROOT%." -ConfigPath "%ROOT%config.yaml" -RequireBackendPortFree
if errorlevel 1 (
  echo [ERROR] Stale project processes could not be stopped or the backend port is still occupied.
  exit /b 1
)
exit /b 0

:select_frontend_port
set "FRONTEND_PORT="
set "FRONTEND_PORT_FILE=%RUN_DIR%\frontend.port.tmp"
del /f /q "%FRONTEND_PORT_FILE%" >nul 2>nul
echo [SETUP] Checking up to 10 frontend ports from config.yaml...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\select-frontend-port.ps1" -ConfigPath "%ROOT%config.yaml" -MaxAttempts 10 > "%FRONTEND_PORT_FILE%"
if errorlevel 1 (
  echo [ERROR] Could not find an available frontend port in 10 attempts.
  del /f /q "%FRONTEND_PORT_FILE%" >nul 2>nul
  exit /b 1
)
set /p FRONTEND_PORT=<"%FRONTEND_PORT_FILE%"
del /f /q "%FRONTEND_PORT_FILE%" >nul 2>nul
if not defined FRONTEND_PORT (
  echo [ERROR] Frontend port selection returned no port.
  exit /b 1
)
echo [SETUP] Frontend will use port %FRONTEND_PORT%.
exit /b 0

:ensure_python
call :find_python_313
if defined PYTHON_CMD goto verify_python

echo [SETUP] Python 3.13 was not found. Installing it now...
where winget >nul 2>nul
if errorlevel 1 goto install_python_from_web

winget install --id Python.Python.3.13 --exact --scope user --source winget --accept-package-agreements --accept-source-agreements --silent --force
if not errorlevel 1 goto verify_python_install
echo [WARN] winget could not install Python 3.13. Trying the official Python installer...

:install_python_from_web
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $listing=Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/'; $versions=[regex]::Matches($listing.Content, '(3\.13\.\d+)/') | ForEach-Object { $_.Groups[1].Value } | Sort-Object { [version]$_ } -Descending -Unique; $version=$versions | Select-Object -First 1; if (-not $version) { throw 'Could not find the latest Python 3.13 release.' }; $suffix=if ([Environment]::Is64BitOperatingSystem) { '-amd64' } else { '' }; $installer=Join-Path $env:TEMP ('python-' + [Guid]::NewGuid().ToString('N') + '.exe'); try { $url='https://www.python.org/ftp/python/' + $version + '/python-' + $version + $suffix + '.exe'; Write-Host ('[SETUP] Downloading ' + $url); Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $installer; $signature=Get-AuthenticodeSignature -FilePath $installer; if ($signature.Status -ne 'Valid') { throw ('Invalid installer signature: ' + $signature.Status) }; $process=Start-Process -FilePath $installer -ArgumentList @('/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_pip=1', 'Include_launcher=1', 'Include_test=0', 'Shortcuts=0') -Wait -PassThru; if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) { throw ('Python installer exited with code ' + $process.ExitCode) } } finally { Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue }"
if errorlevel 1 exit /b 1

:verify_python_install
call :find_python_313
if not defined PYTHON_CMD exit /b 1

:verify_python
echo [SETUP] checking Python 3.13 runtime...
%PYTHON_CMD% -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 13))" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was found but Python 3.13 could not run.
  exit /b 1
)
echo [SETUP] Python 3.13 is ready.
exit /b 0

:find_python_313
set "PYTHON_CMD="
call :refresh_path
if defined PYTHON_BIN (
  %PYTHON_BIN% -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 13))" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=%PYTHON_BIN%"
)
if not defined PYTHON_CMD where py >nul 2>nul && py -3.13 -c "import sys" >nul 2>nul && set "PYTHON_CMD=py -3.13"
if not defined PYTHON_CMD where python3.13 >nul 2>nul && python3.13 -c "import sys" >nul 2>nul && set "PYTHON_CMD=python3.13"
if not defined PYTHON_CMD if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
  "%LocalAppData%\Programs\Python\Python313\python.exe" -c "import sys" >nul 2>nul
  if not errorlevel 1 set PYTHON_CMD="%LocalAppData%\Programs\Python\Python313\python.exe"
)
if not defined PYTHON_CMD if exist "%ProgramFiles%\Python313\python.exe" (
  "%ProgramFiles%\Python313\python.exe" -c "import sys" >nul 2>nul
  if not errorlevel 1 set PYTHON_CMD="%ProgramFiles%\Python313\python.exe"
)
if not defined PYTHON_CMD where python >nul 2>nul && python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 13))" >nul 2>nul && set "PYTHON_CMD=python"
exit /b 0

:ensure_node
call :refresh_path
where node >nul 2>nul
if errorlevel 1 goto install_node
where npm.cmd >nul 2>nul
if not errorlevel 1 goto verify_node

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
echo [SETUP] checking Node.js runtime...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath 'node.exe' -ArgumentList '--version' -WindowStyle Hidden -PassThru; if (-not $p.WaitForExit(30000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; exit 124 }; exit $p.ExitCode"
if errorlevel 1 (
  echo [ERROR] Node.js was found but could not run within 30 seconds on this Windows version.
  exit /b 1
)
echo [SETUP] checking npm runtime...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath $env:ComSpec -ArgumentList @('/d','/c','npm.cmd --version') -WindowStyle Hidden -PassThru; if (-not $p.WaitForExit(30000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; exit 124 }; exit $p.ExitCode"
if errorlevel 1 (
  echo [ERROR] npm was found but could not run within 30 seconds on this Windows version.
  exit /b 1
)
for /f "delims=" %%V in ('node --version') do echo [SETUP] Node.js %%V is ready.
for /f "delims=" %%V in ('npm.cmd --version') do echo [SETUP] npm %%V is ready.
exit /b 0

:refresh_path
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%PATH%;%%P"
if exist "%ProgramFiles%\nodejs\npm.cmd" set "PATH=%ProgramFiles%\nodejs;%PATH%"
if exist "%LocalAppData%\Programs\nodejs\npm.cmd" set "PATH=%LocalAppData%\Programs\nodejs;%PATH%"
exit /b 0
