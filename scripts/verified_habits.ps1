<#
.SYNOPSIS
    Zero-token standalone Hybrid VLM + RAM Verified Habit Rollup (ADR-001).

.EXAMPLE
    .\scripts\verified_habits.ps1 -Username player1
    .\scripts\verified_habits.ps1 -Username player1 -SeedCount 24 -MaxProHabits 4
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Username,
    [int]$SeedCount = 20,
    [int]$MaxPerSet = 2,
    [int]$MinHpLoss = 16,
    [int]$MaxProHabits = 4,
    [int]$ProCandidatesPerHabit = 2,
    [int]$ClipWorkers = 3,
    [int]$GeminiWorkers = 2,
    [switch]$SkipProSearch,
    [string]$PythonExe = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "data")
)

$ErrorActionPreference = "Stop"

$argsList = @(
    "-m", "fightcade_analysis.hybrid_habits",
    "--username", $Username,
    "--personal-dir", (Join-Path $DataDir "personal"),
    "--parquet-dir", (Join-Path $DataDir "parquet"),
    "--data-dir", $DataDir,
    "--out-dir", (Join-Path $DataDir "reports"),
    "--seed-count", "$SeedCount",
    "--max-per-set", "$MaxPerSet",
    "--min-hp-loss", "$MinHpLoss",
    "--max-pro-habits", "$MaxProHabits",
    "--pro-candidates-per-habit", "$ProCandidatesPerHabit",
    "--clip-workers", "$ClipWorkers",
    "--gemini-workers", "$GeminiWorkers"
)
if ($SkipProSearch) {
    $argsList += "--skip-pro-search"
}

& $PythonExe @argsList
exit $LASTEXITCODE
