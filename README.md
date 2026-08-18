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

## Session 2 — historical backfill + spread census (laptop only)

Do **not** run these on the VPS. `data/backfill.sqlite` is laptop scratch;
`data/heartbeat.sqlite` is live-logger evidence. The two files must not mix.

```bash
pip install -r requirements-analysis.txt
```

Order is binding:

```bash
# 1. Probe the live tier — paste the raw JSON back before going further
python -m ingestion.kalshi_history --probe

# 2. Dry-run — market count + volume/time estimate; stops
python -m ingestion.kalshi_history --dry-run

# 3. Probe the historical tier with a pre-cutoff ticker from step 2
python -m ingestion.kalshi_history --probe --ticker KXHIGHNY-24JUL04-T90

# 4. Bulk history — only after both probe readouts are confirmed
python -m ingestion.kalshi_history

# 5. CLINYC labels — run after step 4, not alongside it
python -m ingestion.cli_labels

# 6. Validate that change-emitted candles omit only uneventful periods
python -m analysis.validate_candles

# 7. Locate the venue convention changeover dates
python -m analysis.venue_eras

# 8. Census — gated on K1 v3 ratified in the root chat (date filled in); not on the
#    pre-registered exact-equality VOLUME_RECONCILE FAIL (v3 accepts capture at 99.995%)
python -m analysis.spread_census
```

Steps 4 and 5 share `data/backfill.sqlite`, which is opened with plain
`sqlite3.connect` — no WAL, no `busy_timeout`. Two writers risk
`database is locked`, and serialising costs about two minutes since CLINYC is
roughly 80 monthly requests at 1 rps.

Step 8 is gated on **K1 v3 ratified in the root chat** (see [`knowledge/plan.md`](knowledge/plan.md)
§3), not on the pre-registered exact-equality `VOLUME_RECONCILE` FAIL. v3 accepts capture at
9,317/9,364 markets and names two robustness columns: `_strict15` (15-minute staleness) and
`_exclnoreconcile` (47 volume-mismatch markets excluded). The census makes **carry-forward the
primary statistic**: every metric that depends on the staleness rule appears as
`*_carryforward` (primary), `*_strict15` (robustness a), and `*_exclnoreconcile` (robustness b).
That is only sound if a tier omits a period because nothing happened rather than because data is
missing. `validate_candles.py` tests that with two gates and prints PASS/FAIL for each; v3
records the VOLUME_RECONCILE residual as venue bookkeeping, not a census blocker. If either
robustness column disagrees with the primary on kill-direction, the verdict is deferred.

Run step 6 only after step 4 finishes. A market whose candles are still being
fetched is indistinguishable there from one whose candles are missing.

Steps 6 and 7 read raw JSONL only and open no database, so they are safe to
re-run at any time.

`--probe` prints raw `/historical/cutoff`, one market object, and one candlestick
page, then stops after a single markets page.

Step 1 settled the **live** tier: nested `yes_bid`/`yes_ask`/`price` with
`*_dollars` strings, `volume_fp`, `end_period_ts` epoch seconds. Step 3 exists
because `/historical/markets/{ticker}/candlesticks` serves most of a five-year
backfill and has never returned a payload; pass a pre-cutoff ticker (settled
before `market_settled_ts`) so routing picks the historical path.

`--dry-run` enumerates markets only. It is also the HIGHNY test: if the legacy
series returns zero markets, pre-rename history is missing and the correct
legacy ticker must be found before bulk, not after. If the estimate exceeds
~20 GB or ~24 h at the configured rate cap, the prepared fallback is restricting
candles to `[T−72h, close]` per market. That decision is made at readout, not by
the tool.

Resume: kill -9 and re-run either backfill; completed markets/months are skipped.
Writer repairs a truncated gzip tail on reopen.

CLINYC parser stores `time_of_high_raw` and `time_col_label` verbatim. Clock B
in the census carries ±1 h uncertainty until summer LST-vs-LDT is checked
against IEM ASOS hourly KNYC (~3 summer days).

The census counts a snapshot two-sided only when bid ≥ $0.01 and ask ≤ $0.99.
Kalshi renders an empty book as bid 0.00 / ask 1.00; see
[`knowledge/venue-facts.md`](knowledge/venue-facts.md) §1.4.

`venue_eras.py` reports the two convention changeovers that bound era-spanning
comparisons: the last trading time moved from 11:59 PM civil ET to a fixed
04:59Z between climate days 2026-03-17 and 2026-03-18
([`venue-facts.md`](knowledge/venue-facts.md) §1.8), and the settlement snapshot
moved from 10:00 AM to 7/8 AM ET between 2024-09-03 and 2024-09-04 (§1.10).
Before the first change the T−1h census column falls after close on every EDT
day, which is 58% of climate days — structurally empty, not illiquid.

## Session 2.5 — instrument calibration + forward validation (laptop only)

```bash
# A1. ASOS observations for Clock B calibration (sample from 2025-05-01)
python -m ingestion.asos_obs

# A2. Clock B measurement table (LST vs LDT hypotheses; no verdict in code)
python -m analysis.clockb_check

# B. Window-vs-climate-day mismatch rate (K2 prep)
python -m analysis.window_mismatch

# C. Forward quote-emission cross-check — run when VPS logger is alive
python -m analysis.validate_emission_forward \
  --data-dir /path/to/data/raw --start 2026-08-01 --end 2026-08-03
```

Item C reads `data/raw` orderbook JSONL only and never opens `heartbeat.sqlite`.
Set `climate.cli_time_convention` in `ingestion/config.yaml` (`lst`, `ldt`, or
`unknown`, default) before relying on Clock B or window-mismatch headline numbers.
Census execution remains gated on K1 v3 ratification in the root chat.

## Session 3 — depth census (K1 2b), forward emission, Polymarket logger

```bash
# A. Prospective depth census from logger orderbook JSONL (not candlesticks)
python -m analysis.depth_census --data-dir data/raw

# B. Forward quote-emission cross-check (v3 standing obligation)
python -m analysis.validate_emission_forward \
  --data-dir data/raw --start 2026-08-01 --end 2026-08-03

# C. Polymarket US logger (isolated heartbeat + pm_* raw categories)
python -m ingestion.polymarket_logger --probe   # verify endpoints + one book raw JSON
python -m ingestion.polymarket_logger --once
python -m ingestion.polymarket_logger --status
```

Depth census history is **logger-length only** (days). Re-run as the VPS logger accrues.
Polymarket writes to `data/polymarket_heartbeat.sqlite` and `pm_orderbook` / `pm_markets`
under `data/raw/` — it does not touch the Kalshi heartbeat DB.

On VPS, enable the second unit:

```bash
systemctl --user enable --now deploy/polymarket-logger.service
```

After deploying the Kalshi logger with deeper books, consider raising `api.orderbook_depth`
in `ingestion/config.yaml` so multi-level depth metrics are meaningful.

