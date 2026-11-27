param(
    [string]$QuarkId = "",
    [string]$Label = "manual",
    [ValidateSet("quick", "single", "full", "compare")]
    [string]$Suite = "quick",
    [int]$Workers = 4,
    [string]$Encoder = "",
    [switch]$NoAudio,
    [switch]$NoOverlay,
    [switch]$NoBatch
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "py" }

$argsList = @("-m", "fightcade_analysis.benchmark", "--label", $Label, "--suite", $Suite, "--workers", "$Workers")
if ($QuarkId) { $argsList += @("--quark-id", $QuarkId) }
if ($Encoder) { $argsList += @("--encoder", $Encoder) }
if ($NoAudio) { $argsList += "--no-audio" }
if ($NoOverlay) { $argsList += "--no-overlay" }
if ($NoBatch) { $argsList += "--no-batch" }

if ($Py -eq "py") {
    & py -3.13 @argsList
} else {
    & $Py @argsList
}
exit $LASTEXITCODE
