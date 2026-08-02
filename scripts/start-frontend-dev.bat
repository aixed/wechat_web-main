@echo off
setlocal EnableExtensions

call :refresh_node_path

where node.exe >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js was not found. Please install Node.js and try again.
  exit /b 1
)

where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo [ERROR] npm was not found. Please install Node.js and try again.
  exit /b 1
)

set "FRONTEND_DIR=%~1"
if not defined FRONTEND_DIR set "FRONTEND_DIR=%~dp0..\frontend"

cd /d "%FRONTEND_DIR%"
if errorlevel 1 (
  echo [ERROR] Could not enter frontend directory: %FRONTEND_DIR%
  exit /b 1
)

if not exist "package.json" (
  echo [ERROR] package.json was not found in %CD%.
  exit /b 1
)

if not exist "node_modules\vite\bin\vite.js" (
  echo [SETUP] installing frontend dependencies
  call npm.cmd install
  if errorlevel 1 exit /b 1
)

if not exist "node_modules\vite\bin\vite.js" (
  echo [ERROR] Vite is still missing after npm install.
  echo [ERROR] Please check npm output above.
  exit /b 1
)

node.exe node_modules\vite\bin\vite.js
exit /b %ERRORLEVEL%

:refresh_node_path
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%PATH%;%%P"
if defined ProgramW6432 if exist "%ProgramW6432%\nodejs\node.exe" set "PATH=%ProgramW6432%\nodejs;%PATH%"
if exist "%ProgramFiles%\nodejs\node.exe" set "PATH=%ProgramFiles%\nodejs;%PATH%"
if exist "%LocalAppData%\Programs\nodejs\node.exe" set "PATH=%LocalAppData%\Programs\nodejs;%PATH%"
exit /b 0
