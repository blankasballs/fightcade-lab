<#
.SYNOPSIS
    Instant <1s Replay Takeover with deterministic recorded opponent playback (SF6 / Tekken 8 style).

.DESCRIPTION
    Boots cached `.fs` savestate + `lua/replay_takeover.lua` so the exact match setup replays
    live for ~1.5s, hands you live control right before t0 (or immediately when you press any button),
    and keeps replaying the opponent's exact recorded match inputs so you can drill the interaction!

    Hotkeys inside fcadefbneo:
      Alt+1 : Instant 1-frame retry from savestate
      Alt+2 : Toggle TAKEOVER vs WATCH ORIGINAL (replays both players)
      Alt+3 : Toggle AUTO-LOOP ON/OFF

.EXAMPLE
    .\scripts\takeover.ps1 -QuarkId 1795443936010-2584 -Frame 16938 -Player 1 -Label "Corner Blockstring Defense"
    .\scripts\takeover.ps1 -SelectionId 1795443936010-2584_p1_gf16800_16980
    .\scripts\takeover.ps1 -LatestSelection
#>
[CmdletBinding()]
param(
    [string]$QuarkId = "",
    [int]$Frame = 0,

    [string]$SelectionId = "",
    [switch]$LatestSelection,

    [ValidateSet(1, 2)]
    [int]$Player = 1,

    [string]$GameId = "sfiii3nr1",
    [int]$LeadFrames = 120,
    [int]$TakeoverLead = 30,
    [ValidateSet("takeover", "watch")]
    [string]$Mode = "takeover",
    [switch]$NoAutoLoop,
    [double]$Speed = 1.0,
    [ValidateSet("", "all", "takeover_only")]
    [string]$SpeedScope = "",
    [int]$LoopDelay = -1,
    [switch]$NoStartPaused,
    [string]$Label = "",
    [string]$SavestateOut = "",
    [string]$SavestateFile = "",
    [string]$InputsFileOverride = "",
    [string]$StreamTarget = "",
    [switch]$ForceStream,
    [string]$FbneoExe = "",
    [string]$ReplayTakeoverLua = "",
    [string]$SeekLuaScript = "",
    [string]$PythonExe = "",
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ReplayTakeoverLua) { $ReplayTakeoverLua = Join-Path $repoRoot "lua\replay_takeover.lua" }
if (-not $SeekLuaScript)     { $SeekLuaScript     = Join-Path $repoRoot "lua\seek_and_takeover.lua" }
if (-not $PythonExe)         { $PythonExe         = Join-Path $repoRoot ".venv\Scripts\python.exe" }
if (-not $DataDir)           { $DataDir           = Join-Path $repoRoot "data" }

if (-not $FbneoExe) {
    $fbCandidates = @(
        $env:FC_FBNEO_DIR,
        (Join-Path $env:USERPROFILE "Documents\Fightcade\emulator\fbneo"),
        (Join-Path $env:USERPROFILE "OneDrive\Documents\Fightcade\emulator\fbneo"),
        (Join-Path $env:LOCALAPPDATA "Fightcade\emulator\fbneo"),
        "C:\Fightcade\emulator\fbneo"
    )
    foreach ($dir in $fbCandidates) {
        if (-not $dir) { continue }
        $fastCand = Join-Path $dir "fcadefbneo_fast.exe"
        $stockCand = Join-Path $dir "fcadefbneo.exe"
        if (Test-Path -LiteralPath $fastCand)  { $FbneoExe = $fastCand; break }
        if (Test-Path -LiteralPath $stockCand) { $FbneoExe = $stockCand; break }
    }
    if (-not $FbneoExe) {
        $FbneoExe = Join-Path $env:USERPROFILE "Documents\Fightcade\emulator\fbneo\fcadefbneo.exe"
    }
}

if (-not (Test-Path -LiteralPath $FbneoExe)) {
    throw "fcadefbneo executable not found at: $FbneoExe"
}

$isFastExe = ([System.IO.Path]::GetFileName($FbneoExe).ToLowerInvariant() -eq "fcadefbneo_fast.exe")
function Format-FbneoArg([string]$ArgVal) {
    if ($ArgVal -notmatch "\s") { return $ArgVal }
    if ($isFastExe) { return "`"$ArgVal`"" }
    if (Test-Path -LiteralPath $ArgVal) {
        $fso = New-Object -ComObject Scripting.FileSystemObject
        $short = $fso.GetFile($ArgVal).ShortPath
        if ($short -and ($short -notmatch "\s")) { return $short }
    }
    throw "Path contains whitespace ('$ArgVal') and stock fcadefbneo.exe truncates quoted args (install fcadefbneo_fast.exe)."
}

$explicitTakeoverGf = 0
if ($LatestSelection -or $SelectionId) {
    $bmPath = Join-Path (Join-Path $DataDir "selections") "bookmarks.jsonl"
    if (-not (Test-Path -LiteralPath $bmPath)) {
        throw "No manual selections found at: $bmPath (run .\scripts\annotate.ps1 first)"
    }
    $bms = @(Get-Content -LiteralPath $bmPath | Where-Object { $_.Trim() -ne "" } | ForEach-Object { $_ | ConvertFrom-Json })
    if ($bms.Count -eq 0) {
        throw "Bookmarks file is empty: $bmPath"
    }
    $sel = if ($LatestSelection) {
        $bms[$bms.Count - 1]
    } else {
        $bms | Where-Object { $_.id -eq $SelectionId } | Select-Object -Last 1
    }
    if (-not $sel) {
        throw "SelectionId '$SelectionId' not found in $bmPath"
    }
    $QuarkId = [string]$sel.quark_id
    $Frame = [int]$sel.start_gf
    $Player = [int]$sel.perspective
    if (-not $SavestateFile) { $SavestateFile = [string]$sel.savestate }
    if (-not $InputsFileOverride) { $InputsFileOverride = [string]$sel.inputs }
    if (-not $Label) {
        $noteSuffix = if ($sel.note) { " - $($sel.note)" } else { "" }
        $Label = "Manual Selection gf$($sel.start_gf)..gf$($sel.end_gf)$noteSuffix"
    }
    # Manual selections start right at start_gf, so hand control after a brief 0.35s breath (or immediately on any input!)
    $explicitTakeoverGf = [int]$sel.start_gf + 20
}

if (-not $QuarkId -or $Frame -le 0) {
    throw "Provide either (-QuarkId and -Frame) or (-SelectionId / -LatestSelection)."
}

$fbneoDir = Split-Path -Parent $FbneoExe

# Fast path: instant <1s boot from cached .fs savestate + deterministic recorded input playback
$savestatesDir = Join-Path $DataDir "savestates"
$cachedFs = if ($SavestateFile) { $SavestateFile } else { Join-Path $savestatesDir "${QuarkId}_gf${Frame}.fs" }
$inputsFile = if ($InputsFileOverride) { $InputsFileOverride } else { Join-Path $savestatesDir "${QuarkId}_gf${Frame}.inputs" }

if (-not $ForceStream -and (Test-Path -LiteralPath $cachedFs)) {
    $fsArg = Format-FbneoArg $cachedFs
    $luaArg = Format-FbneoArg $ReplayTakeoverLua

    # Auto-export deterministic recorded inputs from Parquet if sidecar doesn't exist yet (<0.2s)
    if (-not (Test-Path -LiteralPath $inputsFile)) {
        Write-Host "Exporting recorded match inputs for ${QuarkId} @ gf${Frame}..."
        & $PythonExe -m fightcade_analysis.clip_renderer --quark-id $QuarkId --frame $Frame --export-inputs-only
    }

    $env:FC_INPUTS_PATH    = if (Test-Path -LiteralPath $inputsFile) { $inputsFile } else { "" }
    $env:FC_SAVESTATE_PATH = $cachedFs
    $env:FC_TARGET_FRAME   = "$Frame"
    $env:FC_TAKEOVER_GF    = "$explicitTakeoverGf"
    $env:FC_USER_PLAYER    = "$Player"
    $env:FC_TAKEOVER_LEAD  = "$TakeoverLead"
    $env:FC_AUTO_LOOP      = if ($NoAutoLoop) { "0" } else { "1" }
    $env:FC_WATCH_ONLY     = if ($Mode -eq "watch") { "1" } else { "0" }
    $env:FC_SPEED          = "$Speed"
    if ($SpeedScope) { $env:FC_SPEED_SCOPE = $SpeedScope }
    if ($LoopDelay -ge 0) { $env:FC_LOOP_DELAY = "$LoopDelay" }
    if ($NoStartPaused) { $env:FC_START_PAUSED = "0" }
    $env:FC_LABEL          = $Label
    $grouflonDir = Join-Path $fbneoDir "3rd_training_lua-master"
    if (Test-Path -LiteralPath (Join-Path $grouflonDir "src\startup.lua")) {
        $env:FC_GROUFLON_TRAINING = "1"
        $env:FC_GROUFLON_DIR      = $grouflonDir
        $selDir = Join-Path $DataDir "selections"
        if (-not (Test-Path -LiteralPath $selDir)) { New-Item -ItemType Directory -Path $selDir | Out-Null }
        $env:FC_TRAINING_SETTINGS = Join-Path $selDir "takeover_training.cfg"
    } else {
        $env:FC_GROUFLON_TRAINING = "0"
    }

    Write-Host "Instant Replay Takeover (<1s): $cachedFs (You = P$Player, Speed = ${Speed}x, Opponent replaying match)"
    Write-Host "Hotkeys: Select (P1 Coin) or Alt+1 = Retry | Alt+2 Watch | Alt+4 Swap | Alt+5 Sandbox | Alt+6/7/8 Speed | Start Menu"
    # Savestate MUST precede Lua script so FBNeo initializes the driver & loads state before Lua starts
    Start-Process -FilePath $FbneoExe -ArgumentList "-w $fsArg $luaArg" -WorkingDirectory $fbneoDir -WindowStyle Normal
    exit 0
}

if (-not (Test-Path -LiteralPath $SeekLuaScript)) {
    throw "Lua script not found at: $SeekLuaScript"
}

# Fallback path: stream replay + turbo fast-forward via seek_and_takeover.lua
if (-not $StreamTarget) {
    if (Test-Path -LiteralPath $DataDir) {
        foreach ($jf in (Get-ChildItem -LiteralPath $DataDir -Filter "*.json" -ErrorAction SilentlyContinue)) {
            try {
                $doc = Get-Content -LiteralPath $jf.FullName -Raw | ConvertFrom-Json
                $hit = $doc.summaries | Where-Object { $_.quarkid -eq $QuarkId -and $_.stream_target } | Select-Object -First 1
                if ($hit) {
                    $StreamTarget = $hit.stream_target
                    break
                }
            } catch {}
        }
    }
}

if (-not $StreamTarget) {
    Write-Host "Resolving stream target for $QuarkId..."
    $StreamTarget = (& $PythonExe -m fightcade_analysis.fetch_replays --gameid $GameId --resolve-one $QuarkId).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $StreamTarget) {
        throw "Failed to resolve stream target for $QuarkId"
    }
}

$streamArg = Format-FbneoArg $StreamTarget
$seekLuaArg = Format-FbneoArg $SeekLuaScript

$env:FC_TARGET_FRAME   = "$Frame"
$env:FC_LEAD_FRAMES    = "$LeadFrames"
$env:FC_TAKEOVER_MODE  = $Mode
$env:FC_LABEL          = $Label
$env:FC_SAVESTATE_PATH = if ($SavestateOut) { $SavestateOut } else { $cachedFs }

$savDir = Split-Path -Parent $env:FC_SAVESTATE_PATH
if ($savDir -and -not (Test-Path -LiteralPath $savDir)) {
    New-Item -ItemType Directory -Path $savDir -Force | Out-Null
}

$argString = "-w $streamArg $seekLuaArg"

Write-Host "Streaming $QuarkId @ frame $Frame (lead=$LeadFrames, mode=$Mode)"
Write-Host "Saving cached .fs savestate to $($env:FC_SAVESTATE_PATH) for instant future retries."
Write-Host "Hotkeys: Alt+1 = Retry savestate | Alt+2 = Takeover"

Start-Process -FilePath $FbneoExe -ArgumentList $argString -WorkingDirectory $fbneoDir -WindowStyle Normal
