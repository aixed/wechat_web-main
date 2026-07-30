[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [string]$DefaultKey = "admin"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "config.yaml was not found: $ConfigPath"
}

$encoding = New-Object System.Text.UTF8Encoding($false)
$text = [System.IO.File]::ReadAllText($ConfigPath, $encoding)
$linePattern = '(?m)^(?<prefix>\s*web_access_key\s*:\s*)(?<value>[^#\r\n]*)(?<suffix>\s*(?:#.*)?)$'
$match = [regex]::Match($text, $linePattern)

function Normalize-KeyValue {
    param([string]$Value)

    $trimmed = [string]$Value
    $trimmed = $trimmed.Trim()
    if ($trimmed.Length -ge 2) {
        $first = $trimmed.Substring(0, 1)
        $last = $trimmed.Substring($trimmed.Length - 1, 1)
        if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
            $trimmed = $trimmed.Substring(1, $trimmed.Length - 2).Trim()
        }
    }
    return $trimmed
}

$changed = $false
$usesDefault = $false

if ($match.Success) {
    $value = Normalize-KeyValue -Value $match.Groups["value"].Value
    $isMissing = (
        -not $value -or
        $value -eq "~" -or
        $value.Equals("null", [System.StringComparison]::OrdinalIgnoreCase)
    )

    if ($isMissing) {
        $text = [regex]::Replace(
            $text,
            $linePattern,
            { param($m) $m.Groups["prefix"].Value + '"' + $DefaultKey + '"' + $m.Groups["suffix"].Value },
            1
        )
        [System.IO.File]::WriteAllText($ConfigPath, $text, $encoding)
        $changed = $true
        $usesDefault = $true
    } elseif ($value -eq $DefaultKey) {
        $usesDefault = $true
    }
} else {
    $lineEnding = if ($text -match "`r`n") { "`r`n" } else { "`n" }
    if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) {
        $text += $lineEnding
    }
    $text += "web_access_key: `"$DefaultKey`"$lineEnding"
    [System.IO.File]::WriteAllText($ConfigPath, $text, $encoding)
    $changed = $true
    $usesDefault = $true
}

if ($changed) {
    Write-Host "[SETUP] web_access_key was not configured. Set default value: $DefaultKey"
}

if ($usesDefault) {
    Write-Host "[WARN] Using default web access key: $DefaultKey"
    Write-Host "[WARN] Please edit config.yaml and change the web_access_key parameter before exposing this service."
}
