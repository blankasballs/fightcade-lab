<#
.SYNOPSIS
    Zero-token standalone Multi-Set Personal Habit & Leak Rollup builder.

.EXAMPLE
    .\scripts\habit_rollup.ps1 -Username player1
    .\scripts\habit_rollup.ps1 -Username player1 -NoSummarize
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Username,
    [int]$TopNPerSet = 25,
    [double]$MinRegret = 0.06,
    [int]$MaxClipHabits = 6,
    [int]$ClipsPerHabit = 3,
    [int]$ProClipsPerHabit = 0,
    [int]$ClipWorkers = 3,
    [switch]$NoClips,
    [switch]$NoSummarize,
    [string]$PythonExe = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "data")
)

$ErrorActionPreference = "Stop"

$argsList = @(
    "-m", "fightcade_analysis.habit_rollup",
    "--username", $Username,
    "--personal-dir", (Join-Path $DataDir "personal"),
    "--parquet-dir", (Join-Path $DataDir "parquet"),
    "--data-dir", $DataDir,
    "--out-dir", (Join-Path $DataDir "reports"),
    "--top-n-per-set", "$TopNPerSet",
    "--min-regret", "$MinRegret",
    "--max-clip-habits", "$MaxClipHabits",
    "--clips-per-habit", "$ClipsPerHabit",
    "--pro-clips-per-habit", "$ProClipsPerHabit",
    "--clip-workers", "$ClipWorkers"
)
if (-not $NoClips) {
    $argsList += "--render-clips"
}
if (-not $NoSummarize) {
    $argsList += "--summarize"
}

& $PythonExe @argsList
exit $LASTEXITCODE
