@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

set "ROOT=%~dp0"
set "RUN_DIR=%ROOT%.run"

echo Stopping WeChat Web backend/frontend...

for %%F in ("%RUN_DIR%\backend.pid" "%RUN_DIR%\frontend.pid") do (
  if exist "%%~F" (
    set "PID="
    set /p PID=<"%%~F"
    if defined PID (
      taskkill /PID !PID! /T /F >nul 2>nul
    )
    del /f /q "%%~F" >nul 2>nul
  )
)
del /f /q "%RUN_DIR%\frontend.port" "%RUN_DIR%\start-all-parent.pid" >nul 2>nul

taskkill /FI "WINDOWTITLE eq WeChat Web Backend*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq WeChat Web Frontend*" /T /F >nul 2>nul

set "STOP_ROOT=%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=[IO.Path]::GetFullPath($env:STOP_ROOT).TrimEnd('\'); $self=Get-CimInstance Win32_Process -Filter \"ProcessId=$PID\"; $exclude=@($PID,$self.ParentProcessId); $escaped=[regex]::Escape($root); $targets=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($exclude -notcontains $_.ProcessId) -and ((($_.CommandLine -match $escaped) -and ($_.CommandLine -match 'backend[\\/]+main\.py' -or $_.CommandLine -match 'frontend.*(vite|npm(\.cmd)?\s+run\s+dev)')) -or ($_.CommandLine -match 'uvicorn\s+main:app')) }; foreach ($p in $targets) { Write-Host ('Stopping process pid=' + $p.ProcessId + ' ' + $p.Name); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Done.
endlocal
