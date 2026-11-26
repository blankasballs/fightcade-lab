<#
.SYNOPSIS
    Launch fcadefbneo.exe on a replay (quark:stream, quark:replay, or .fr) with dump_telemetry.lua.

.NOTES
    IMPORTANT: Fightcade's FBNeo ProcessCmdLine() has an off-by-one quote bug that truncates
    the last character of double-quoted CLI arguments. Keep all paths passed to fcadefbneo.exe
    free of spaces and pass them unquoted!
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReplayTarget, # e.g. "quark:stream,sfiii3nr1,1728857242828-5771.7,7100" or "C:\path\to\match.fr"

    [Parameter(Mandatory = $true)]
    [string]$OutputCsv,

    [string]$FbneoExe = "",
    [string]$FastFbneoExe = "",
    [string]$LuaScript = "",
    [string]$PythonExe = "",
    [string]$RoundCacheDir = "",
    [int]$TimeoutSeconds = 300,
    [int]$IdleFinishSeconds = 15,
    [int]$MaxFrames = 0,
    [int]$PostMatchIdle = 3600,
    [ValidateSet("Hidden", "Normal", "Minimized")]
    [string]$WindowStyle = "Hidden",
    [switch]$NormalSpeed,
    [switch]$NoAutoExit,
    [switch]$NoMute,
    [switch]$StockExe
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $LuaScript) { $LuaScript = Join-Path $repoRoot "lua\dump_telemetry.lua" }
if (-not $PythonExe) { $PythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe" }

function Find-FightcadeFbneoDir {
    $candidates = @(
        $env:FC_FBNEO_DIR,
        (Join-Path $env:USERPROFILE "Documents\Fightcade\emulator\fbneo"),
        (Join-Path $env:USERPROFILE "OneDrive\Documents\Fightcade\emulator\fbneo"),
        (Join-Path $env:LOCALAPPDATA "Fightcade\emulator\fbneo"),
        "C:\Fightcade\emulator\fbneo"
    )
    foreach ($c in $candidates) {
        if ($c -and ((Test-Path -LiteralPath (Join-Path $c "fcadefbneo.exe")) -or (Test-Path -LiteralPath (Join-Path $c "fcadefbneo_fast.exe")))) {
            return $c
        }
    }
    return (Join-Path $env:USERPROFILE "Documents\Fightcade\emulator\fbneo")
}

$fbneoRoot = Find-FightcadeFbneoDir
if (-not $FbneoExe)     { $FbneoExe     = Join-Path $fbneoRoot "fcadefbneo.exe" }
if (-not $FastFbneoExe) { $FastFbneoExe = Join-Path $fbneoRoot "fcadefbneo_fast.exe" }

$useFast = (-not $StockExe) -and (-not $NormalSpeed) -and (Test-Path -LiteralPath $FastFbneoExe)
$effectiveExe = if ($useFast) { $FastFbneoExe } else { $FbneoExe }

if (-not (Test-Path -LiteralPath $effectiveExe)) {
    throw "fcadefbneo executable not found at: $effectiveExe"
}
if (-not (Test-Path -LiteralPath $LuaScript)) {
    throw "Lua script not found at: $LuaScript"
}

function Format-FbneoArg([string]$ArgVal, [bool]$IsFast) {
    if ($ArgVal -notmatch "\s") { return $ArgVal }
    if ($IsFast) { return "`"$ArgVal`"" }
    if (Test-Path -LiteralPath $ArgVal) {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        $short = if (Test-Path -LiteralPath $ArgVal -PathType Container) { $fso.GetFolder($ArgVal).ShortPath } else { $fso.GetFile($ArgVal).ShortPath }
        if ($short -and ($short -notmatch "\s")) { return $short }
    }
    throw "Path contains whitespace ('$ArgVal') and stock fcadefbneo.exe truncates quoted args (install fcadefbneo_fast.exe)."
}

$outDir = Split-Path -Parent $OutputCsv
if ($outDir -and -not (Test-Path -LiteralPath $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$env:FC_TELEMETRY_OUT   = $OutputCsv
$env:FC_TURBO           = if ($NormalSpeed) { "0" } else { "1" }
$env:FC_AUTO_EXIT       = if ($NoAutoExit) { "0" } else { "1" }
$env:FC_POST_MATCH_IDLE = "$PostMatchIdle"
if ($RoundCacheDir) {
    New-Item -ItemType Directory -Path $RoundCacheDir -Force | Out-Null
    $env:FC_ROUND_CACHE_DIR = $RoundCacheDir
} else {
    $env:FC_ROUND_CACHE_DIR = ""
}
if ($MaxFrames -gt 0) {
    $env:FC_MAX_FRAMES = "$MaxFrames"
}

$fbneoDir = Split-Path -Parent $effectiveExe
# ReplayTarget MUST precede LuaScript so FBNeo initializes the driver before loading Lua
$maxSpeedArg = if ($useFast) { "-headless " } else { "" }
$targetArg = Format-FbneoArg $ReplayTarget $useFast
$luaArg    = Format-FbneoArg $LuaScript $useFast
$argString = "-w ${maxSpeedArg}$targetArg $luaArg"

Write-Host "Launching: $effectiveExe $argString"
$proc = Start-Process -FilePath $effectiveExe -ArgumentList $argString -WorkingDirectory $fbneoDir -WindowStyle $WindowStyle -PassThru

# Mute ONLY this background fcadefbneo.exe PID via Windows WASAPI when using stock exe (fast exe is natively silent)
if (-not $useFast -and -not $NoMute -and (Test-Path -LiteralPath $PythonExe)) {
    Start-Process -FilePath $PythonExe -ArgumentList "-m fightcade_analysis.audio_mute --pid $($proc.Id) --wait-seconds 15" -WindowStyle Hidden | Out-Null
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$lastSize = -1L
$idleSince = $null
$finishedByIdle = $false
$timedOut = $false

while (-not $proc.HasExited) {
    if ($sw.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
        $timedOut = $true
        break
    }

    if ($IdleFinishSeconds -gt 0 -and -not $NoAutoExit -and (Test-Path -LiteralPath $OutputCsv)) {
        $curSize = (Get-Item -LiteralPath $OutputCsv).Length
        if ($curSize -gt 1024) {
            if ($curSize -ne $lastSize) {
                $lastSize = $curSize
                $idleSince = $sw.Elapsed
            } elseif ($idleSince -ne $null -and ($sw.Elapsed - $idleSince).TotalSeconds -ge $IdleFinishSeconds) {
                $finishedByIdle = $true
                break
            }
        }
    }

    Start-Sleep -Seconds 1
}

if ($finishedByIdle) {
    Write-Host "CSV output quiescent for ${IdleFinishSeconds}s (${lastSize} bytes); stream reached EOF. Closing PID $($proc.Id)."
    try { $proc.Kill() } catch {}
    $proc.WaitForExit(5000) | Out-Null
    $exitCode = 0
} elseif ($timedOut) {
    Write-Warning "fcadefbneo.exe exceeded timeout (${TimeoutSeconds}s); terminating PID $($proc.Id)."
    try { $proc.Kill() } catch {}
    $proc.WaitForExit(5000) | Out-Null
    $exitCode = 124
} else {
    $exitCode = $proc.ExitCode
    Write-Host "fcadefbneo.exe exited with code $exitCode."
}

$errPath = "$OutputCsv.err"
if (Test-Path -LiteralPath $errPath) {
    Write-Warning "Lua error log present at ${errPath}:"
    Get-Content -LiteralPath $errPath
}

exit $exitCode
