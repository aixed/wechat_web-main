@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"

echo Stopping WeChat Web backend/frontend...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\stop-project-processes.ps1" -RootPath "%ROOT%." -ConfigPath "%ROOT%config.yaml" -RequireBackendPortFree
if errorlevel 1 (
  echo [ERROR] Some project processes could not be stopped or the backend port is still occupied.
  endlocal
  exit /b 1
)

echo Done.
endlocal
exit /b 0
