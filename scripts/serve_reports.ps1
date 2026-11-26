<#
.SYNOPSIS
    Start the local Fightcade Lab HTTP server (defaults to 127.0.0.1:8765 for zero Firewall popups).

.DESCRIPTION
    Serves Fightcade Lab Workbench, Replay Studio, and 60fps MP4 clips with full HTTP 206 byte-range
    streaming. Defaults to localhost-only (`127.0.0.1:8765`); pass `-Lan` or `-HostAddr 0.0.0.0` to
    enable Tailscale / LAN phone access. Pass `-EnableProCorpus` (or set `FC_ENABLE_PRO_CORPUS=1`)
    to unlock personal local Pro Corpus Search.

.EXAMPLE
    .\scripts\serve_reports.ps1
    .\scripts\serve_reports.ps1 -Lan
    .\scripts\serve_reports.ps1 -Lan -EnableProCorpus
#>
[CmdletBinding()]
param(
    [string]$HostAddr = "127.0.0.1",
    [switch]$Lan,
    [switch]$EnableProCorpus,
    [int]$Port = 8765,
    [string]$PythonExe = "",
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonExe) { $PythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe" }
if (-not $DataDir)   { $DataDir   = Join-Path $repoRoot "data" }

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python venv executable not found at: $PythonExe"
}

if ($Lan) {
    $HostAddr = "0.0.0.0"
}
if ($EnableProCorpus) {
    $env:FC_ENABLE_PRO_CORPUS = "1"
}

Write-Host "Starting Fightcade Lab HTTP Server on http://${HostAddr}:${Port}/ ..."
if ($HostAddr -eq "0.0.0.0") {
    Write-Host "LAN/Tailscale mode active. If your phone cannot connect, allow TCP port $Port in Windows Firewall:"
    Write-Host "  New-NetFirewallRule -DisplayName 'Fightcade Lab HTTP' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow"
} else {
    Write-Host "Localhost-only bind (zero Windows Firewall prompts). Pass -Lan for Tailscale/LAN phone access."
}
Write-Host "Tip: Touching data\.reload triggers an automatic clean restart inside this interactive window."
Write-Host ""

while ($true) {
    $cliArgs = @("-m", "fightcade_analysis.server", "--host", $HostAddr, "--port", "$Port", "--data-dir", $DataDir)
    if ($Lan) { $cliArgs += "--lan" }
    & $PythonExe @cliArgs
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 75) {
        Write-Host ""
        Write-Host "[serve_reports] Reload requested (exit 75) - restarting server in interactive desktop..." -ForegroundColor Cyan
        Start-Sleep -Milliseconds 300
        continue
    }
    exit $exitCode
}
