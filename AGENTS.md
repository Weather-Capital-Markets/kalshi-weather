# AGENTS.md

## Cursor Cloud specific instructions

This repo is a Python 3.11+ CLI project (a Kalshi KXHIGHNY market-data logger). There is no web UI, server, or Docker; all "services" are command-line entry points run via `python -m`. Standard commands live in `README.md`; only non-obvious cloud notes are captured here.

### Environment

- Dependencies are installed into a project-local virtualenv at `.venv` (created by the startup update script). Prefix commands with `.venv/bin/` (e.g. `.venv/bin/pytest`) or activate with `source .venv/bin/activate`. `.venv`, `.env`, and `data/` are gitignored.
- The system package `python3-venv` (`python3.12-venv` on this base image) is required for `python3 -m venv` to work and is assumed present in the environment.
- `requirements-dev.txt` covers runtime + lint/test tooling; `requirements-analysis.txt` adds `pandas`/`matplotlib` for the analysis/backfill lane. The update script installs both so every entry point is runnable.

### Lint / test

- Lint: `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`. The repo also has a `pre-commit` config (gitleaks + ruff); `pre-commit run --all-files` downloads hook repos on first run and needs network.
- Tests: `.venv/bin/pytest -q` (33 tests). Tests mock HTTP and make no live API calls. The matplotlib `PyparsingDeprecationWarning`/`DeprecationWarning` noise from `tests/test_spread_census.py` is expected and harmless.

### Running the app (non-obvious caveats)

- The logger polls the **live** Kalshi API at `external-api.kalshi.com`, so running it end-to-end requires outbound network egress. Endpoints are used unauthenticated; the stubbed `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` env vars are not needed for the market-data logger.
- One-shot smoke test: `.venv/bin/python -m ingestion.kalshi_logger --once`. Verify with `.venv/bin/python -m ingestion.kalshi_logger --status` and `sqlite3 data/heartbeat.sqlite "SELECT COUNT(*) FROM poll_attempts;"`.
- DB separation is a hard rule enforced by convention: the live logger only touches `data/heartbeat.sqlite`; the Session 2 backfill lane (`ingestion.kalshi_history`) writes `data/backfill.sqlite`. Do not mix the two.
