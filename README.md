# kalshi-weather

Research and trading system for Kalshi KXHIGHNY/KNYC Central Park daily
maximum-temperature markets. Session 1 delivers a production-quality market-data
logger; analysis, models, and weather ingestion come in later sessions.

## Requirements

- Python 3.11+
- Ubuntu 22.04+ (for VPS deploy) or any OS for local dev
- Network access to `external-api.kalshi.com`

## Quick start (local)

```bash
git clone https://github.com/YOUR_USER/kalshi-weather.git
cd kalshi-weather

python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env               # optional; auth unused in session 1
pre-commit install
pre-commit run --all-files
```

## Run the logger

One-shot smoke test (requires network):

```bash
python -m ingestion.kalshi_logger --once
```

Continuous daemon:

```bash
python -m ingestion.kalshi_logger
```

Last-hour heartbeat summary:

```bash
python -m ingestion.kalshi_logger --status
```

## Verify data is being written

After `--once` or a few minutes of daemon operation:

```bash
# Heartbeat rows (every HTTP attempt, success or failure)
sqlite3 data/heartbeat.sqlite "SELECT COUNT(*) FROM poll_attempts;"
sqlite3 data/heartbeat.sqlite "SELECT ts_utc, endpoint, ticker, ok, http_status FROM poll_attempts ORDER BY ts_utc DESC LIMIT 10;"

# Raw captures (one JSON object per line, gzip compressed)
find data/raw -name '*.jsonl.gz' | head
python -c "import gzip,json; p=next(__import__('pathlib').Path('data/raw').rglob('orderbook/*.jsonl.gz')); print(json.loads(gzip.open(p,'rt').readline()))"
```

Expected after a successful `--once`:

- At least one line in `data/raw/YYYY-MM-DD/markets/KXHIGHNY.jsonl.gz`
- At least one line per active market in `data/raw/YYYY-MM-DD/orderbook/<ticker>.jsonl.gz`
- Rows in `poll_attempts` for every HTTP call

## Configuration

Edit [`ingestion/config.yaml`](ingestion/config.yaml):

| Key | Default | Purpose |
|-----|---------|---------|
| `api.base_url` | `https://external-api.kalshi.com/trade-api/v2` | API root (alt: `https://api.elections.kalshi.com/trade-api/v2`) |
| `api.paths.*` | see file | One-line path fixes if Kalshi changes routes |
| `series` | `KXHIGHNY` | Series tickers to poll |
| `cadence_sec.markets` | 300 | Market discovery interval |
| `cadence_sec.orderbook` | 5 | Orderbook poll interval |
| `cadence_sec.trades` | 60 | Trades poll interval |
| `cadence_sec.settlement` | 86400 | Settlement refresh interval |

The orderbook cadence is measured between completed polling sweeps. With 12 open
markets and a 5 requests/second global cap, one sweep takes at least 2.4 seconds;
larger open sets stretch the effective cadence. Use heartbeat rows to measure the
observed interval before changing the cap. Kalshi's published rate limit remains
`TODO(verify)` in the venue research lane.

## Storage layout

```
data/
├── raw/
│   └── YYYY-MM-DD/
│       ├── markets/KXHIGHNY.jsonl.gz
│       ├── orderbook/KXHIGHNY-....jsonl.gz
│       └── trades/KXHIGHNY.jsonl.gz
└── heartbeat.sqlite
```

Each JSONL line:

```json
{"ts_utc":"...","endpoint":"...","http_status":200,"latency_ms":42,"payload":{...}}
```

All timestamps are UTC ISO 8601 at capture time.

## Restart and crash safety

- **SIGTERM**: logger finishes in-flight writes and closes files cleanly.
- **kill -9**: prior complete lines are intact (each line is flushed + fsync'd); the last line may be truncated — readers should skip malformed tail lines.
- **Trades cursor**: persisted in `heartbeat.sqlite` (`kv_state` table); resumes after restart without re-fetching from the beginning.
- **Completed trades sweep**: clears its pagination cursor so the next scheduled poll starts at the newest page. Cursor advancement occurs only after the raw page is fsync'd. Trade IDs are committed immediately afterward; a crash between those commits can duplicate a page but cannot lose one.

Test restart safety:

```bash
python -m ingestion.kalshi_logger &          # start daemon
sleep 30
kill -9 $!                                     # simulate hard crash
python -m ingestion.kalshi_logger --once       # should resume trades cursor
sqlite3 data/heartbeat.sqlite "SELECT key, value FROM kv_state WHERE key LIKE 'trades_cursor%';"
```

## Deploy to Ubuntu VPS (user-level systemd)

```bash
# On the VPS, after cloning and setting up the venv (see Quick start)
mkdir -p ~/.config/systemd/user
cp deploy/kalshi-logger.service ~/.config/systemd/user/
# Edit WorkingDirectory/ExecStart paths if not using ~/kalshi-weather

systemctl --user daemon-reload
systemctl --user enable kalshi-logger
systemctl --user start kalshi-logger

# Verify
systemctl --user status kalshi-logger
journalctl --user -u kalshi-logger -f
python -m ingestion.kalshi_logger --status
sqlite3 ~/kalshi-weather/data/heartbeat.sqlite "SELECT COUNT(*) FROM poll_attempts;"
```

Enable lingering so the user service survives logout:

```bash
sudo loginctl enable-linger $USER
```

## Tests

```bash
pytest -q
```

Tests mock HTTP; no live API calls in CI.

## Project layout

```
knowledge/          Source-of-truth docs (weather, venue facts)
ingestion/          Kalshi market-data logger
tests/              Unit tests
deploy/             systemd unit file
data/               Runtime captures (gitignored)
```

## Acceptance checklist

- [ ] `python -m ingestion.kalshi_logger --once` writes orderbook + markets + heartbeat rows
- [ ] kill -9 mid-run, restart: no corrupted prior lines, trades cursor resumes
- [ ] `pre-commit run --all-files` passes (gitleaks + ruff)
- [ ] `data/` and `.env` excluded from git; `.env.example` present
- [ ] This README runs top-to-bottom on a fresh machine

## API notes

Market-data endpoints are used unauthenticated. If any return 401, the logger records it in heartbeat and continues. Auth env vars (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`) are stubbed for a future session.

Official docs: https://docs.kalshi.com/getting_started/quick_start_market_data

Both documented production hostnames were verified to return KXHIGHNY market JSON
on 2026-08-14. The logger defaults to `external-api.kalshi.com`;
`api.elections.kalshi.com` remains a config-only alternate.
