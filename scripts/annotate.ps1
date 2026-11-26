<#
.SYNOPSIS
    Watch any Fightcade replay live in fcadefbneo.exe and manually mark [START .. END]
    sequences for instant Replay Takeover and full-sequence semantic corpus search.

.DESCRIPTION
    Controls inside fcadefbneo (work live AND while paused):
      Space         : Pause / Resume
      , / .         : Rewind / Step  -/+ 5 frames   (0.08s — pinpoint)
      Left / Right  : Rewind / Step  -/+ 20 frames  (0.33s — 1 move)
      - / = (PgUp/Dn): Rewind / Step -/+ 100 frames (1.67s — full setup)
      Tab           : Toggle TURBO fast-forward (skip intros/rounds)
      [             : Mark START of interaction (immediately saves .fs savestate)
      ]             : Mark END of interaction (finalizes .fs, .inputs + 90f tail, and bookmark)
      Backspace     : Cancel current START mark

.EXAMPLE
    .\scripts\annotate.ps1 -QuarkId 1795443936010-2584 -Player 1
    .\scripts\annotate.ps1 -QuarkId 1795443936010-2584 -Player 1 -Note "Alex corner trap"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$QuarkId,

    [ValidateSet(1, 2)]
    [int]$Player = 1,

    [string]$GameId = "sfiii3nr1",
    [string]$Note = "",
    [string]$StreamTarget = "",
    [string]$FbneoExe = (Join-Path $env:USERPROFILE "Documents\Fightcade\emulator\fbneo\fcadefbneo.exe"),
    [string]$AnnotateLua = (Join-Path (Split-Path -Parent $PSScriptRoot) "lua\annotate_replay.lua"),
    [string]$PythonExe = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "data")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $FbneoExe)) {
    throw "fcadefbneo.exe not found at: $FbneoExe"
}
if (-not (Test-Path -LiteralPath $AnnotateLua)) {
    throw "Lua script not found at: $AnnotateLua"
}
if ($AnnotateLua -match "\s") {
    throw "AnnotateLua path contains whitespace ('$AnnotateLua'), which triggers FBNeo's CLI quote-truncation bug."
}

$selectionsDir = Join-Path $DataDir "selections"
if (-not (Test-Path -LiteralPath $selectionsDir)) {
    New-Item -ItemType Directory -Path $selectionsDir -Force | Out-Null
}
$bookmarksPath = Join-Path $selectionsDir "bookmarks.jsonl"
$beforeCount = if (Test-Path -LiteralPath $bookmarksPath) { (Get-Content -LiteralPath $bookmarksPath).Count } else { 0 }

if (-not $StreamTarget) {
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

if (-not $StreamTarget) {
    Write-Host "Resolving stream target for $QuarkId..."
    $StreamTarget = (& $PythonExe -m fightcade_analysis.fetch_replays --gameid $GameId --resolve-one $QuarkId).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $StreamTarget) {
        throw "Failed to resolve stream target for $QuarkId"
    }
}

if ($StreamTarget -match "\s") {
    throw "StreamTarget contains whitespace ('$StreamTarget'), which triggers FBNeo's CLI quote-truncation bug."
}

$env:FC_QUARK_ID       = $QuarkId
$env:FC_USER_PLAYER    = "$Player"
$env:FC_SELECTIONS_DIR = $selectionsDir
$env:FC_NOTE           = $Note

$fbneoDir = Split-Path -Parent $FbneoExe
Write-Host "Launching live replay annotator for $QuarkId (P$Player)..."
Write-Host "Scrub  : Space=Pause | ,/.=-/+5f | Left/Right=-/+20f | -/==(PgUp/PgDn)=-/+100f | Tab=Turbo"
Write-Host "Mark   : [ = Mark START | ] = Mark END | Backspace = Cancel Start"
Write-Host "Close fcadefbneo when finished to see next-step Takeover & Semantic Search commands."

$proc = Start-Process -FilePath $FbneoExe -ArgumentList "-w $StreamTarget $AnnotateLua" -WorkingDirectory $fbneoDir -WindowStyle Normal -PassThru
$proc.WaitForExit()

if (Test-Path -LiteralPath $bookmarksPath) {
    $allLines = @(Get-Content -LiteralPath $bookmarksPath | Where-Object { $_.Trim() -ne "" })
    if ($allLines.Count -gt $beforeCount) {
        $newLines = $allLines[$beforeCount..($allLines.Count - 1)]
        Write-Host "`nSaved $($newLines.Count) new bookmark(s):" -ForegroundColor Green
        foreach ($ln in $newLines) {
            $bm = $ln | ConvertFrom-Json
            Write-Host "  * $($bm.id) (gf$($bm.start_gf)..gf$($bm.end_gf), $($bm.duration_s)s)"
            Write-Host "    Takeover : .\scripts\takeover.ps1 -SelectionId $($bm.id)"
            Write-Host "    Search   : .\.venv\Scripts\python.exe -m fightcade_analysis.sequence_search --selection-id $($bm.id) --render-clips --summarize"
        }
    } else {
        Write-Host "`nNo new bookmarks were marked in this session."
    }
}
