#!/usr/bin/env bash
# Cloud Agent install: system deps + Python venv for kalshi-weather.
# Idempotent: safe to re-run against a partially prepared or cached VM.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> system packages (apt)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
# python3-venv/build tools: create venv and build the eccodes cffi bindings.
# libeccodes*: the ecCodes C library that cfgrib/eccodes load at runtime.
# sqlite3: heartbeat DB inspection used throughout the README.
sudo apt-get install -y -qq --no-install-recommends \
  python3-venv python3-dev build-essential \
  libeccodes0 libeccodes-dev \
  sqlite3 git curl

echo "==> python venv (.venv)"
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> python dependencies (dev + analysis)"
python -m pip install --upgrade pip
pip install -r requirements-dev.txt -r requirements-analysis.txt

echo "==> cfgrib/ecCodes self-check"
python -m cfgrib selfcheck

echo "install complete. Activate with: source .venv/bin/activate"
