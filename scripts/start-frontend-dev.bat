@echo off
setlocal EnableExtensions

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
