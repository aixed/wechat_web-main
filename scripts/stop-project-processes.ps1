[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RootPath,

    [string]$ConfigPath = "",

    [switch]$RequireBackendPortFree,

    [ValidateRange(1, 60)]
    [int]$WaitSeconds = 10
)

$ErrorActionPreference = "Stop"
$normalizedRoot = [System.IO.Path]::GetFullPath($RootPath).TrimEnd('\')
$rootPattern = [regex]::Escape($normalizedRoot)
$runDirectory = Join-Path $normalizedRoot ".run"

$excludedPids = @{}
$currentPid = $PID
while ($currentPid -gt 0 -and -not $excludedPids.ContainsKey($currentPid)) {
    $excludedPids[$currentPid] = $true
    $currentProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$currentPid" -ErrorAction SilentlyContinue
    if ($null -eq $currentProcess) {
        break
    }
    $currentPid = [int]$currentProcess.ParentProcessId
}

function Test-ProjectProcess {
    param($Process)

    $processId = [int]$Process.ProcessId
    if ($excludedPids.ContainsKey($processId)) {
        return $false
    }

    $commandLine = [string]$Process.CommandLine
    $executablePath = [string]$Process.ExecutablePath
    if (-not $commandLine -and -not $executablePath) {
        return $false
    }

    $belongsToRoot = $commandLine -match $rootPattern
    if (-not $belongsToRoot -and $executablePath) {
        $belongsToRoot = $executablePath.StartsWith(
            $normalizedRoot + '\',
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    if (-not $belongsToRoot) {
        return $false
    }

    $isBackend = (
        $commandLine -match '(?i)\bmain\.py\b' -and
        ($commandLine -match '(?i)[\\/]backend(?:[\\/]|["\s])' -or
         $executablePath -match '(?i)[\\/]backend[\\/]')
    ) -or $commandLine -match '(?i)uvicorn(?:\.exe)?\s+main:app'

    $isFrontend = (
        $commandLine -match '(?i)[\\/]frontend(?:[\\/]|["\s])' -and
        $commandLine -match '(?i)(?:vite(?:\.js)?|npm(?:\.cmd)?\s+run\s+dev)'
    )

    return $isBackend -or $isFrontend
}

function Get-ProjectProcesses {
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { Test-ProjectProcess -Process $_ }
    )
}

function Stop-ProcessTree {
    param($Process)

    $processId = [int]$Process.ProcessId
    Write-Host ("[STOP] pid={0} name={1}" -f $processId, $Process.Name)
    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    & $taskkill /PID $processId /T /F 2>&1 | Out-Null
    if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

for ($round = 0; $round -lt 4; $round++) {
    $targets = @(Get-ProjectProcesses)
    if ($targets.Count -eq 0) {
        break
    }
    foreach ($target in $targets) {
        Stop-ProcessTree -Process $target
    }
    Start-Sleep -Milliseconds 500
}

$remaining = @(Get-ProjectProcesses)
if ($remaining.Count -gt 0) {
    $ids = ($remaining | ForEach-Object { $_.ProcessId }) -join ", "
    throw "Project processes are still running after cleanup: $ids"
}

foreach ($name in @(
    "backend.pid",
    "frontend.pid",
    "frontend.port",
    "frontend.port.tmp",
    "start-all-parent.pid"
)) {
    Remove-Item -LiteralPath (Join-Path $runDirectory $name) -Force -ErrorAction SilentlyContinue
}

function Read-BackendPort {
    if (-not $ConfigPath -or -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        return 5000
    }

    $configText = [System.IO.File]::ReadAllText($ConfigPath, [System.Text.Encoding]::UTF8)
    $portPattern = New-Object System.Text.RegularExpressions.Regex(
        '(?m)^\s*server_port\s*:\s*["'']?([0-9]+)["'']?\s*(?:#.*)?$'
    )
    $portMatch = $portPattern.Match($configText)
    if (-not $portMatch.Success) {
        return 5000
    }

    $port = [int]$portMatch.Groups[1].Value
    if ($port -lt 1 -or $port -gt 65535) {
        throw "server_port in config.yaml must be between 1 and 65535."
    }
    return $port
}

function Test-TcpPortAvailable {
    param([int]$Port)

    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener -ArgumentList ([System.Net.IPAddress]::Any), $Port
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) {
            $listener.Stop()
        }
    }
}

function Get-ListeningProcessIds {
    param([int]$Port)

    $netstat = Join-Path $env:SystemRoot "System32\netstat.exe"
    $pattern = '^\s*TCP\s+\S+:' + [regex]::Escape([string]$Port) + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
    return @(
        & $netstat -ano -p tcp |
            ForEach-Object {
                if ($_ -match $pattern) {
                    [int]$Matches[1]
                }
            } |
            Sort-Object -Unique
    )
}

if ($RequireBackendPortFree) {
    $backendPort = Read-BackendPort
    $deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
    while (-not (Test-TcpPortAvailable -Port $backendPort) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }

    if (-not (Test-TcpPortAvailable -Port $backendPort)) {
        $owners = @(Get-ListeningProcessIds -Port $backendPort)
        $ownerText = if ($owners.Count -gt 0) { $owners -join ", " } else { "unknown" }
        throw "Backend port $backendPort is still occupied by PID(s): $ownerText"
    }
    Write-Host "[READY] Backend port $backendPort is available."
}
