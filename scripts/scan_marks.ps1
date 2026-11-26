<#
.SYNOPSIS
    Scan your latest (or specified) Fightcade match for in-game Select (Coin) marks and populate the Takeover Queue.

.DESCRIPTION
    While playing live on Fightcade, tap Select (Coin) whenever a sequence gives you trouble.
    Run this script right after the match (or let `serve_reports.ps1`'s auto-watcher run it automatically)
    to build instant <1s Replay Takeover savestates anchored 6 seconds before each Select press.

.EXAMPLE
    .\scripts\scan_marks.ps1 -Username player1
    .\scripts\scan_marks.ps1 -QuarkId 1795531097416-1969 -Player 1 -LookbackSeconds 6
#>
[CmdletBinding()]
param(
    [string]$Username = "",
    [string]$QuarkId = "",
    [ValidateSet(1, 2)]
    [int]$Player = 1,
    [double]$LookbackSeconds = 6.0,
    [string]$PythonExe = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "data")
)

$ErrorActionPreference = "Stop"

$argsList = @(
    "-m", "fightcade_analysis.select_queue",
    "--lookback-seconds", "$LookbackSeconds",
    "--data-dir", $DataDir
)
if ($Username) { $argsList += @("--username", $Username) }
if ($QuarkId)  { $argsList += @("--quark-id", $QuarkId, "--player", "$Player") }

& $PythonExe @argsList
exit $LASTEXITCODE
