# Session 6c — NBM forecast vs Kalshi market at T-24h (Windows PowerShell).
# Run from anywhere:
#   scripts\run-session6c.cmd
# Or:
#   powershell -ExecutionPolicy Bypass -File .\scripts\run-session6c.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path (Join-Path $RepoRoot "analysis\forecast_vs_market.py"))) {
    Write-Error "analysis\forecast_vs_market.py missing. Clone/pull kalshi-weather and checkout cursor/session6c-forecast-vs-market-1b11"
}

if (-not (Test-Path $Python)) {
    Write-Error @"
Virtualenv not found at $Python

  cd $RepoRoot
  py -3.12 -m venv .venv
  .\.venv\Scripts\pip install -r requirements.txt -r requirements-analysis.txt
"@
}

function Test-DataPrereq {
    param([string]$GlobPattern, [string]$Label)
    $hits = @(Get-ChildItem -Path (Join-Path $RepoRoot "data\raw") -Recurse -Filter $GlobPattern -ErrorAction SilentlyContinue)
    if ($hits.Count -eq 0) {
        Write-Warning "MISSING: no $Label under data\raw\ — Session 6c needs laptop bulk backfill (markets_history + candlesticks), not VPS orderbook logs."
        return $false
    }
    Write-Host "OK: $($hits.Count) $Label file(s) under data\raw\" -ForegroundColor Green
    return $true
}

$decoded = Join-Path $RepoRoot "data\nbm\decoded_v441"
if (-not (Test-Path $decoded)) {
    Write-Warning "MISSING: data\nbm\decoded_v441\ — run ingestion.nbm_archive on branch cursor/nbm-vintage-v441-7e-1b11 (PR #26) or rsync from cloud agent."
} else {
    $n = @(Get-ChildItem -Path $decoded -Filter "*.parquet").Count
    Write-Host "OK: $n parquet files in decoded_v441\" -ForegroundColor Green
}

$hasMarkets = Test-DataPrereq "*.jsonl.gz" "markets_history"
$hasCandles = Test-DataPrereq "*.jsonl.gz" "candlesticks"
if (-not $hasMarkets -or -not $hasCandles) {
    Write-Host @"

Session 6c cannot run on the VPS logger alone.
Bulk historical fetch (kalshi_history) is laptop-only — see README.md Session 2.

On the machine that ran: python -m ingestion.kalshi_history
  dir data\raw\*\markets_history\*.jsonl.gz
  dir data\raw\*\candlesticks\*.jsonl.gz

"@ -ForegroundColor Yellow
    exit 1
}

function Invoke-Step {
    param([string]$Name)
    Write-Host ">> python -m $Name" -ForegroundColor Cyan
    & $Python -m $Name
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Invoke-Step "analysis.bracket_enumeration"
Invoke-Step "analysis.forecast_vs_market"
Write-Host "Done. See analysis\out\forecast_vs_market_summary.csv" -ForegroundColor Green
