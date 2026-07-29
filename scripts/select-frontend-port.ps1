[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [ValidateRange(1, 100)]
    [int]$MaxAttempts = 10
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Config file not found: $ConfigPath"
}

$utf8 = New-Object System.Text.UTF8Encoding($false)
$configText = [System.IO.File]::ReadAllText($ConfigPath, [System.Text.Encoding]::UTF8)
$portValuePattern = New-Object System.Text.RegularExpressions.Regex(
    '(?m)^\s*frontend_port\s*:\s*["'']?([0-9]+)["'']?\s*(?:#.*)?$'
)
$portKeyPattern = New-Object System.Text.RegularExpressions.Regex(
    '(?m)^\s*frontend_port\s*:'
)
$portMatch = $portValuePattern.Match($configText)

if ($portMatch.Success) {
    $startPort = [int]$portMatch.Groups[1].Value
} elseif ($portKeyPattern.IsMatch($configText)) {
    throw "frontend_port in config.yaml must be an integer between 1 and 65535."
} else {
    $startPort = 80
}

if ($startPort -lt 1 -or $startPort -gt 65535) {
    throw "frontend_port in config.yaml must be between 1 and 65535."
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

$selectedPort = 0
for ($offset = 0; $offset -lt $MaxAttempts; $offset++) {
    $candidatePort = $startPort + $offset
    if ($candidatePort -gt 65535) {
        break
    }
    if (Test-TcpPortAvailable -Port $candidatePort) {
        $selectedPort = $candidatePort
        break
    }
}

if ($selectedPort -eq 0) {
    $lastPort = [Math]::Min(65535, $startPort + $MaxAttempts - 1)
    throw "No available frontend port was found from $startPort to $lastPort."
}

$portLinePattern = New-Object System.Text.RegularExpressions.Regex(
    '(?m)^\s*frontend_port\s*:.*$'
)
$portLine = "frontend_port: $selectedPort"
if ($portLinePattern.IsMatch($configText)) {
    $updatedConfig = $portLinePattern.Replace($configText, $portLine, 1)
} else {
    $separator = if ($configText.EndsWith("`n")) { "" } else { [Environment]::NewLine }
    $updatedConfig = $configText + $separator + $portLine + [Environment]::NewLine
}

[System.IO.File]::WriteAllText($ConfigPath, $updatedConfig, $utf8)
Write-Output $selectedPort
