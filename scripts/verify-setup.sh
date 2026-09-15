#!/usr/bin/env bash
# Smoke-test local setup: lint, unit tests, live logger --once, analysis sanity.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
fi

echo "==> ruff"
"$PYTHON" -m ruff check .

echo "==> pytest"
"$PYTHON" -m pytest -q

echo "==> kalshi_logger --once"
"$PYTHON" -m ingestion.kalshi_logger --once

echo "==> kalshi_logger --status"
"$PYTHON" -m ingestion.kalshi_logger --status

echo "==> polymarket_logger --once"
"$PYTHON" -m ingestion.polymarket_logger --once

echo "==> polymarket_logger --status"
"$PYTHON" -m ingestion.polymarket_logger --status

KALSHI_RAW="$("$PYTHON" - <<'PY'
from pathlib import Path
print(len(list(Path("data/raw").rglob("orderbook/*.jsonl.gz"))))
PY
)"
PM_RAW="$("$PYTHON" - <<'PY'
from pathlib import Path
print(len(list(Path("data/raw").rglob("pm_orderbook/*.jsonl.gz"))))
PY
)"

if [[ "$KALSHI_RAW" -lt 1 ]]; then
  echo "FAIL: expected Kalshi orderbook raw files, got $KALSHI_RAW"
  exit 1
fi
if [[ "$PM_RAW" -lt 10 ]]; then
  echo "FAIL: expected Polymarket ladder books (>=10), got $PM_RAW"
  exit 1
fi

echo "==> depth_census (Kalshi orderbook JSONL)"
"$PYTHON" -m analysis.depth_census --data-dir data/raw

echo "OK: kalshi_orderbook_files=$KALSHI_RAW polymarket_book_files=$PM_RAW"
