#!/usr/bin/env bash
# Bootstrap Ubuntu VPS: venv, deps, user-level systemd for both loggers.
# Run from repo root on the VPS after git clone.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REPO_DIR="${REPO_DIR:-$ROOT}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

echo "==> venv + dependencies in $REPO_DIR"
cd "$REPO_DIR"
if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt -r requirements-analysis.txt

echo "==> one-shot logger smoke (before systemd)"
python -m ingestion.kalshi_logger --once
python -m ingestion.polymarket_logger --once

echo "==> install user systemd units"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "$UNIT_DIR"
for unit in kalshi-logger polymarket-logger; do
  sed "s|%h/kalshi-weather|${REPO_DIR}|g" "deploy/${unit}.service" > "${UNIT_DIR}/${unit}.service"
done
systemctl --user daemon-reload
systemctl --user enable kalshi-logger polymarket-logger
systemctl --user restart kalshi-logger polymarket-logger

echo "==> enable linger (run once; may prompt for sudo password)"
if command -v loginctl >/dev/null 2>&1; then
  sudo loginctl enable-linger "$USER" || echo "WARN: loginctl enable-linger failed — run manually"
fi

echo "==> service status"
systemctl --user status kalshi-logger --no-pager || true
systemctl --user status polymarket-logger --no-pager || true

echo "==> heartbeat summaries"
python -m ingestion.kalshi_logger --status
python -m ingestion.polymarket_logger --status

echo "VPS setup complete. Tail logs with:"
echo "  journalctl --user -u kalshi-logger -f"
echo "  journalctl --user -u polymarket-logger -f"
