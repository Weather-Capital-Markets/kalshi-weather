# Session 2.5 calibration scripts (Windows PowerShell).
# Run from the repo root:
#   scripts\run-session25.cmd asos          # no execution-policy change needed
#   .\scripts\run-session25.ps1 -Step asos  # if scripts are allowed
# If .ps1 is blocked: powershell -ExecutionPolicy Bypass -File .\scripts\run-session25.ps1 -Step asos

param(
    [ValidateSet("all", "asos", "clockb", "window")]
    [string]$Step = "all"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$AsosModule = Join-Path $RepoRoot "ingestion\asos_obs.py"

if (-not (Test-Path $AsosModule)) {
    Write-Error @"
ingestion\asos_obs.py is missing. Pull latest main, then retry:

  cd $RepoRoot
  git fetch origin
  git pull origin main
  dir ingestion\asos_obs.py
"@
}

if (-not (Test-Path $Python)) {
    Write-Error "Virtualenv not found at $Python. Create it with: python -m venv .venv"
}

function Invoke-Step {
    param([string]$Name, [string[]]$Args)
    Write-Host ">> python -m $Name $($Args -join ' ')" -ForegroundColor Cyan
    & $Python -m $Name @Args
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Step) {
    "asos"   { Invoke-Step "ingestion.asos_obs" @() }
    "clockb" { Invoke-Step "analysis.clockb_check" @() }
    "window" { Invoke-Step "analysis.window_mismatch" @() }
    default {
        Invoke-Step "ingestion.asos_obs" @()
        Invoke-Step "analysis.clockb_check" @()
        Invoke-Step "analysis.window_mismatch" @()
    }
}
