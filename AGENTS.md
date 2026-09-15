# AGENTS.md

## Cursor Cloud specific instructions

This repo is a Python 3.11+ CLI research project for Kalshi KXHIGHNY / Polymarket
NYC daily-max-temperature markets plus NBM weather ingestion. There is no web UI,
server, or Docker; all "services" are command-line entry points run via `python -m`.
Standard commands and the per-session workflow live in `README.md`; only the
non-obvious cloud notes are captured here.

### Environment

- Dependencies are installed into a project-local virtualenv at `.venv` (created by the startup install script). Prefix commands with `.venv/bin/` (e.g. `.venv/bin/pytest`) or activate with `source .venv/bin/activate`. `.venv`, `.env`, and `data/` are gitignored.
- System packages required (the base image does NOT ship them; the install script apt-installs them):
  - `python3.12-venv` — needed for `python3 -m venv`.
  - `libeccodes0` + `libeccodes-tools` — the ECMWF ecCodes C library. The `eccodes`/`cfgrib` pip packages are only Python bindings; without the C library, importing `cfgrib` fails with `RuntimeError: Cannot find the ecCodes library` and the entire NBM weather lane (`ingestion.nbm_archive`, `analysis.nbm_*`) is unusable.
- `requirements-dev.txt` covers runtime + lint/test tooling; `requirements-analysis.txt` adds the analysis/backfill/weather stack (`pandas`, `matplotlib`, `pyarrow`, `cfgrib`, `eccodes`, `xarray`). The install script installs both so every entry point is runnable.

### Lint / test

- Lint (what the project actually enforces, per `scripts/verify-setup.sh`): `.venv/bin/ruff check .`.
- `.venv/bin/ruff format --check .` currently reports ~12 pre-existing files as needing reformat on `main`; this is not enforced by the project, so do NOT bulk-reformat unrelated files to make it pass.
- Tests: `.venv/bin/pytest -q` (163 tests). Tests mock HTTP and make no live API calls. matplotlib/numpy deprecation + "Mean of empty slice" warnings from the census tests are expected and harmless.

### Running the apps (non-obvious caveats)

- All live entry points hit real APIs and need outbound network egress. Endpoints are used unauthenticated; the stubbed `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` env vars are not needed.
  - Kalshi logger: `external-api.kalshi.com`. `.venv/bin/python -m ingestion.kalshi_logger --once`, status via `--status`.
  - Polymarket logger: `gamma-api.polymarket.com` + `clob.polymarket.com`. `.venv/bin/python -m ingestion.polymarket_logger --once`, status via `--status`.
  - NBM weather: NOAA NBM qmd archive via HTTP byte-range on `.idx` sidecars. Smoke test: `.venv/bin/python -m ingestion.nbm_archive --probe`.
- Canonical end-to-end smoke test: `PYTHON=.venv/bin/python bash scripts/verify-setup.sh` (ruff, pytest, both loggers `--once`/`--status`, and a `depth_census` sanity check).
- DB / storage separation is a hard rule enforced by convention. Do not mix these files:
  - Kalshi live logger → `data/heartbeat.sqlite` (+ `data/raw/**/orderbook/`, `markets/`, `trades/`).
  - Polymarket logger → `data/polymarket_heartbeat.sqlite` (+ `data/raw/**/pm_orderbook/`, `pm_markets/`).
  - Session 2 backfill lane (`ingestion.kalshi_history`) → `data/backfill.sqlite`.
