<#
.SYNOPSIS
    Zero-token one-command personal match analyzer: fetch -> ingest -> diagnose -> 6s clips -> Gemini -> Reports Hub.

.DESCRIPTION
    Give it your Fightcade username (analyzes your latest set automatically, detecting P1/P2 side)
    or a specific QuarkId. Keeps personal Parquets separate under `data\personal\` so they never
    pollute the pro reference corpus in `data\parquet\`.

.EXAMPLE
    .\scripts\analyze_match.ps1 -Username player1
    .\scripts\analyze_match.ps1 -Username player1 -Open
    .\scripts\analyze_match.ps1 -QuarkId 1795443936010-2584 -Player 1 -TopN 5
#>
[CmdletBinding()]
param(
    [string]$Username = "",
    [string]$QuarkId = "",

    [ValidateSet(0, 1, 2)]
    [int]$Player = 0, # 0 = auto-detect from Username (defaults to 1 if only QuarkId given)

    [string]$GameId = "sfiii3nr1",
    [ValidateSet("hybrid", "legacy")]
    [string]$Mode = "hybrid",
    [int]$TopN = 5,
    [int]$ClipWorkers = 3,
    [int]$GeminiWorkers = 2,
    [switch]$NoClips,
    [switch]$NoSummarize,
    [switch]$ForceIngest,
    [switch]$Open,
    [int]$ServerPort = 8765,
    [string]$PythonExe = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [string]$RunReplayScript = (Join-Path $PSScriptRoot "run_replay.ps1"),
    [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "data")
)

$ErrorActionPreference = "Stop"

if (-not $Username -and -not $QuarkId) {
    throw "Provide either -Username <fightcade_handle> or -QuarkId <quarkid>."
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python venv executable not found at: $PythonExe"
}

$streamTarget = ""

# Step 1: Resolve latest match for Username (if provided)
if ($Username) {
    $userManifest = Join-Path $DataDir "${Username}_replays.json"
    Write-Host "=== Step 1/3: Fetching latest $GameId replays for '$Username' ===" -ForegroundColor Cyan
    & $PythonExe -m fightcade_analysis.fetch_replays `
        --gameid $GameId `
        --username $Username `
        --limit 5 `
        --min-duration 30 `
        --max-pages 1 `
        --resolve `
        --merge `
        --out $userManifest
    if ($LASTEXITCODE -ne 0 -and -not (Test-Path -LiteralPath $userManifest)) {
        throw "Failed to fetch replays for $Username"
    }

    $doc = Get-Content -LiteralPath $userManifest -Raw | ConvertFrom-Json
    $summaries = @($doc.summaries | Sort-Object -Property date -Descending)
    $chosen = if ($QuarkId) {
        $summaries | Where-Object { $_.quarkid -eq $QuarkId } | Select-Object -First 1
    } else {
        $summaries | Select-Object -First 1
    }
    if (-not $chosen) {
        throw "No matching replay found for username '$Username' (quarkid='$QuarkId')."
    }
    $QuarkId = [string]$chosen.quarkid
    $streamTarget = [string]$chosen.stream_target
    if ($Player -eq 0) {
        if ($chosen.p1_name -and $chosen.p1_name.ToLower() -eq $Username.ToLower()) {
            $Player = 1
        } elseif ($chosen.p2_name -and $chosen.p2_name.ToLower() -eq $Username.ToLower()) {
            $Player = 2
        } else {
            $Player = 1
        }
    }
    Write-Host "Selected: $QuarkId ($($chosen.p1_name) vs $($chosen.p2_name), dur=$($chosen.duration)s) -> analyzing P$Player" -ForegroundColor Green
} else {
    if ($Player -eq 0) { $Player = 1 }
}

# Step 2: Ingest into data\personal\<QuarkId>.parquet if not already present
$personalDir = Join-Path $DataDir "personal"
$tmpDir = Join-Path $DataDir "tmp_csv"
New-Item -ItemType Directory -Path $personalDir -Force | Out-Null
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

$targetParquet = Join-Path $personalDir "$QuarkId.parquet"
$proParquet = Join-Path (Join-Path $DataDir "parquet") "$QuarkId.parquet"

if (-not $ForceIngest -and (Test-Path -LiteralPath $targetParquet)) {
    Write-Host "`n=== Step 2/3: Reusing existing personal Parquet ($targetParquet) ===" -ForegroundColor Cyan
} elseif (-not $ForceIngest -and (Test-Path -LiteralPath $proParquet)) {
    $targetParquet = $proParquet
    Write-Host "`n=== Step 2/3: Reusing existing Parquet ($targetParquet) ===" -ForegroundColor Cyan
} else {
    Write-Host "`n=== Step 2/3: Ingesting $QuarkId into $targetParquet ===" -ForegroundColor Cyan
    if (-not $streamTarget) {
        $streamTarget = (& $PythonExe -m fightcade_analysis.fetch_replays --gameid $GameId --resolve-one $QuarkId).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $streamTarget) {
            throw "Failed to resolve stream target for $QuarkId"
        }
    }
    $csvPath = Join-Path $tmpDir "$QuarkId.csv"
    $roundCacheDir = Join-Path (Join-Path $DataDir "replay_cache") $QuarkId
    & $RunReplayScript -ReplayTarget $streamTarget -OutputCsv $csvPath -RoundCacheDir $roundCacheDir -IdleFinishSeconds 6
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $csvPath)) {
        throw "Replay ingestion failed for $QuarkId (exit $LASTEXITCODE)"
    }
    & $PythonExe -m fightcade_analysis.csv_to_parquet $csvPath $targetParquet --quark-id $QuarkId --delete-csv
    if ($LASTEXITCODE -ne 0) {
        throw "CSV-to-Parquet conversion failed for $QuarkId"
    }
}

# Step 3: Run Stage 3+4 Coaching Report + Clips + Gemini + Reports Hub update
Write-Host "`n=== Step 3/3: Generating Coaching Report (P$Player, top $TopN moments) ===" -ForegroundColor Cyan
$repArgs = @(
    "-m", "fightcade_analysis.report_ui",
    "--target", $targetParquet,
    "--perspective", "$Player",
    "--parquet-dir", (Join-Path $DataDir "parquet"),
    "--mode", $Mode,
    "--top-n", "$TopN",
    "--max-clip-moments", "$TopN",
    "--clip-workers", "$ClipWorkers",
    "--gemini-workers", "$GeminiWorkers",
    "--out-dir", (Join-Path $DataDir "reports")
)
if (-not $NoClips) { $repArgs += "--render-clips" }
if (-not $NoSummarize) { $repArgs += "--summarize" }

$repOutput = & $PythonExe @repArgs
$repOutput | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    throw "report_ui failed (exit $LASTEXITCODE)"
}

$htmlLine = $repOutput | Where-Object { $_ -match "^Wrote coaching report HTML:\s*(.+)$" } | Select-Object -Last 1
if ($htmlLine -and $htmlLine -match "^Wrote coaching report HTML:\s*(.+)$") {
    $htmlFile = $Matches[1].Trim()
    $leaf = Split-Path -Leaf $htmlFile
    Write-Host "`nReady! Open locally or via your LAN/VPN server:" -ForegroundColor Green
    Write-Host "  File  : $htmlFile"
    Write-Host "  Server: http://127.0.0.1:${ServerPort}/reports/$leaf"
    Write-Host "  Hub   : http://127.0.0.1:${ServerPort}/"
    if ($Open) {
        $serverUrl = "http://127.0.0.1:${ServerPort}/reports/$leaf"
        $useServer = $false
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:${ServerPort}/api/status" -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { $useServer = $true }
        } catch {}
        if ($useServer) {
            Start-Process $serverUrl
        } else {
            Start-Process $htmlFile
        }
    }
}
