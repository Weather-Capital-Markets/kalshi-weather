# Audit: Logger storage efficiency (Session 7d)

**Date:** 2026-08-22  
**Scope:** Measurement and options only — no capture or storage format changes implemented.  
**VPS context:** ~13 days disk headroom at ~260 MB/day write rate.

## Executive summary

| Finding | Value |
|---|---|
| **Dominant category** | `pm_orderbook` (~75% of projected daily writes) |
| **Projected daily rate (live-calibrated)** | **267 MB/day** (matches VPS ~260 MB/day) |
| **Payload redundancy** (consecutive polls, same market) | Kalshi ~79–88%; Polymarket ~84% (live sample) |
| **Envelope redundancy** | **0%** — `ts_utc` changes every poll, so full JSONL lines are never byte-identical |
| **Gzip** | Confirmed per-line in `RawJsonlWriter`; **26–37× worse** than batch gzip for repetitive lines |
| **Top recommendation** | **Store-on-change** for orderbook categories + nightly batch recompact of closed days |

---

## 1. Current storage measurement (importance: critical)

### 1.1 No local production corpus

`data/raw/` is absent in the dev workspace. Measurements below combine:

1. **Live API calibration** (2026-08-22): real Kalshi + Polymarket payloads written through `RawJsonlWriter`.
2. **VPS anchor:** user-reported ~260 MB/day (consistent with model).
3. **Analysis tool:** `scripts/measure_logger_storage.py` for on-box read-only scans.

### 1.2 Capture cadence (from code + config)

| Logger | Task | `cadence_sec` | Notes |
|---|---|---|---|
| Kalshi | `markets` | 300 | One paginated sweep per series (`KXHIGHNY`) |
| Kalshi | `orderbook` | 5 | Between completed sweeps over all open tickers |
| Kalshi | `trades` | 60 | Per-market pagination; empty pages often skipped |
| Kalshi | `settlement` | 86400 | Pending dropped markets |
| Polymarket | `pm_markets` | 300 | Gamma discovery per event day |
| Polymarket | `pm_orderbook` | 5 | CLOB `/book` per open bracket |

Systemd units (`deploy/kalshi-logger.service`, `deploy/polymarket-logger.service`) run both loggers continuously with 1s outer sleep. Effective per-market interval includes sweep duration:

```text
effective_interval ≈ cadence_sec + (open_markets / rate_limit)
```

README notes: with 12 Kalshi markets and 5 req/s cap, one orderbook sweep ≈ 2.4s.

| Venue | Open instruments | Rate cap | Sweep time | Effective OB interval |
|---|---|---|---|---|
| Kalshi | 12 tickers | 5 req/s | 2.4s | **7.4s** → ~11,676 polls/market/day |
| Polymarket | 22 brackets (2 horizon days) | 15 req/s | 1.5s | **6.5s** → ~13,361 polls/bracket/day |

Config: `ingestion/config.yaml` (`cadence_sec`, `polymarket.cadence_sec`). Kalshi `api.orderbook_depth: 0` (top-of-book intent); live API still returned ~1,045-byte payloads (full `orderbook_fp` ladder at API default).

### 1.3 Per-record on-disk sizes (live payloads, gzip per line)

Measured by writing one capture through `RawJsonlWriter` with 2026-08-22 API bodies:

| Category | Payload JSON (bytes) | Gzip on disk (bytes) | Uncompressed envelope (bytes) |
|---|---|---|---|
| `kalshi orderbook` | 1,045 | **402** | ~1,200 |
| `kalshi markets` (full open page) | 27,499 | **2,329** | ~27,700 |
| `kalshi trades` (empty page) | ~30 | **138** | ~170 |
| `pm_orderbook` | 1,859 | **685** | ~2,100 |
| `pm_markets` | ~106,000 | **11,671** | ~107,000 |

Polymarket CLOB books are deep (~37 ask levels in probe sample); Kalshi books are ~1 KB despite `depth=0`.

### 1.4 Daily write projection (math)

```text
daily_bytes(category) = instruments × polls_per_instrument_per_day × bytes_per_gz_record

Kalshi orderbook:  12 × 11,676 × 402  = 56.3 MB/day
Kalshi markets:    1 × 288 × 2,329     = 0.7 MB/day
Kalshi trades:     12 × 11,676 × 138  = 2.3 MB/day   (empty-page dominated)
pm_markets:        2 × 288 × 11,671   = 6.7 MB/day
pm_orderbook:      22 × 13,361 × 685  = 201.3 MB/day
────────────────────────────────────────────────────
TOTAL                                      267.4 MB/day
```

| Category | MB/day | Share |
|---|---|---|
| **pm_orderbook** | **201.3** | **75.3%** |
| kalshi orderbook | 56.3 | 21.1% |
| pm_markets | 6.7 | 2.5% |
| kalshi trades | 2.3 | 0.9% |
| kalshi markets | 0.7 | 0.3% |

At 267 MB/day on a ~3.5 GB free buffer → **~13 days headroom** (matches VPS observation).

**Bytes per poll (blended):** pm_orderbook ~685 B × 22 brackets ≈ **15.1 KB/sweep**; Kalshi orderbook ~402 B × 12 ≈ **4.8 KB/sweep**.

### 1.5 Layout

```text
data/raw/YYYY-MM-DD/<category>/<key>.jsonl.gz
```

Envelope (unchanged contract):

```json
{"ts_utc":"...","endpoint":"...","http_status":200,"latency_ms":42,"payload":{...verbatim API body...}}
```

Writer: `ingestion/writer.py` (`RawJsonlWriter`). Kalshi: `ingestion/kalshi_logger.py`. Polymarket: `ingestion/polymarket_logger.py`.

---

## 2. Redundancy analysis (importance: high)

### 2.1 What “identical” means

| Comparison | Kalshi (8 tickers, 20 rounds, 0.5s) | Polymarket (10 tokens, 20 rounds, 0.5s) |
|---|---|---|
| **Payload JSON** (verbatim API body) | **79.6%** identical consecutive pairs | **83.7%** |
| **Full envelope line** | **0%** (always differs in `ts_utc`) | **0%** |

Production polls every ~6–7s (not 0.5s), so payload-identical fraction is likely **≥85–92%** for quiet brackets.

### 2.2 Why envelope redundancy is always zero

Every poll writes a new `ts_utc` (`utc_now_iso()` in loggers). Even unchanged books produce distinct JSONL lines. **Storage optimization must target payload deduplication or batch compression**, not naive line equality.

### 2.3 Implication for store-on-change

Carry-forward semantics already used in analysis (`book_at_or_before` in `validate_emission_forward.py`): unchanged books need only a timestamp anchor, not a repeated verbatim payload. **~80–90% of orderbook lines are reconstructable from the previous full payload.**

### 2.4 Methodology (appendix script)

Live probe (2026-08-22): poll same instruments every 0.5s for 20 rounds; compare `json.dumps(payload, sort_keys=True)`.

On VPS with accrued data:

```bash
python scripts/measure_logger_storage.py --data-dir data/raw --redundancy-sample 5000
```

Redundancy loop (per file, consecutive records):

```python
payload_b = json.dumps(record["payload"], separators=(",", ":"), sort_keys=True).encode()
if payload_b == prev_payload:
    identical += 1
```

Test fixtures (`tests/fixtures/orderbook.json`) are single snapshots — useful for payload shape, not redundancy rates.

---

## 3. Gzip verification (importance: high)

### 3.1 Confirmed in writer

`ingestion/writer.py` lines 61–67: each record is `json.dumps` → **independent `gzip.GzipFile` member** → append with `fsync`.

Properties:

- Safe same-day restart (truncated tail repair via `_repair_truncated_tail`).
- **Poor compression efficiency** on small, repetitive lines.

### 3.2 Compression ratios

| Payload type | Uncompressed line | Per-line gzip | Ratio | Batch gzip (100 lines) | Per-line / batch |
|---|---|---|---|---|---|
| Top-of-book synthetic | 210 B | 182 B | 1.15× | 4.88 B/line | **37×** overhead |
| Shallow 5-level | 367 B | 221 B | 1.66× | 6.19 B/line | **36×** |
| Deep 50-level | 2,151 B | 592 B | 3.63× | 22.06 B/line | **27×** |
| Live Kalshi OB | ~1,200 B | 402 B | ~3.0× | (est.) ~15 B/line | **~27×** |
| Live PM OB | ~2,100 B | 685 B | ~3.1× | (est.) ~25 B/line | **~27×** |

**Achievable vs actual:** JSON orderbooks compress well in batch (**~50–100×** for long identical runs). Per-member gzip captures **~3×** on live payloads but adds **~20–30× structural overhead** vs a single gzip stream per file/day.

`pm_markets` gzip ratio ~9× (106 KB → 11.7 KB) — large JSON benefits more, but still appended per poll.

---

## 4. Query paths today (emission cross-check contract)

Analysis reads **verbatim payloads** from JSONL only; never `heartbeat.sqlite`.

### 4.1 `validate_emission_forward.py`

```48:77:analysis/validate_emission_forward.py
def load_logger_books(
    data_dir: Path,
    *,
    start: date,
    end: date,
    tickers: set[str] | None = None,
) -> dict[str, list[tuple[datetime, float | None, float | None]]]:
    ...
            for record in read_jsonl_gz(path):
                ...
                bid, ask = top_of_book_from_payload(payload)
                by_ticker[ticker].append((captured, bid, ask))
```

- `book_at_or_before(rows, boundary)` — last poll at or before minute boundary.
- `compare_ticker` — flags **silent book changes** (logger change with no candle).
- Requires: ordered `(ts_utc, payload)` per ticker; payload must support `top_of_book_from_payload`.

### 4.2 `analysis/orderbook.py`

```66:71:analysis/orderbook.py
def top_of_book_from_payload(payload: dict[str, Any]) -> tuple[float | None, float | None]:
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        return None, None
    return _best_yes_bid(book), _best_yes_ask(book)
```

`depth_census.py` uses full `payload` via `depth_metrics_from_payload` — needs complete `orderbook_fp` levels, not just top-of-book.

**Contract:** Raw verbatim API payloads remain the logical contract. Storage optimizations must **reconstruct identical payloads** (or equivalent query views) at any `ts_utc`.

---

## 5. Options (DO NOT IMPLEMENT — projections only)

Baseline: **267 MB/day** (live-calibrated).

Assume production payload-identical rate **R = 90%** for orderbook categories (conservative vs live 0.5s sample).

### Option A: Store-on-change + heartbeat reference

**Mechanism:** On each poll, if `payload` bytes equal previous poll for same `(category, key)`:

- Write compact record: `{"ts_utc", "heartbeat": true, "ref_ts_utc": "<last full>"}` or omit payload with explicit marker.
- On change: write full verbatim payload as today.

**Reconstructability:** Reader walks file in order; on heartbeat, carry forward last full payload (same as `book_at_or_before` logic). `read_jsonl_gz` or a thin adapter expands heartbeats to full envelopes before analysis.

| Category | Full gz (B) | Est. heartbeat gz (B) | Projected MB/day |
|---|---|---|---|
| pm_orderbook | 685 | ~50 | 0.1×685 + 0.9×50 → **26.8 MB** |
| kalshi orderbook | 402 | ~50 | 0.1×402 + 0.9×50 → **9.5 MB** |
| Other | — | — | **9.7 MB** (unchanged) |
| **Total** | | | **~46 MB/day** |

**Savings: ~83%** (267 → 46 MB/day) → **~58 days** headroom at same disk.

**Emission cross-check:** Preserved. `load_logger_books` expands heartbeats; `book_at_or_before` unchanged. Silent-change detection still compares consecutive **effective** books.

**depth_census:** Preserved if heartbeats expand to full `orderbook_fp` before `depth_metrics_from_payload`.

**Risk:** Implementation must handle day boundaries, first record, and crash mid-file; contract stays “verbatim payload at query time.”

### Option B: Higher compression (zstd) or per-day recompaction

#### B1: zstd per line

Replace gzip member with zstd frame (~similar ratio, slightly smaller headers). **Savings ~5–15%** on current layout — does not fix per-record overhead.

**Projected:** ~230–250 MB/day. **Savings ~5–15%.**

#### B2: Nightly recompact of closed UTC days

After day roll, merge `*.jsonl.gz` into **one gzip (or zstd) stream** per file; keep JSONL logical lines inside.

For orderbook files with 90% identical payloads, batch ratio ~**27×** on repetitive content; blended **~10–15×** with changes:

| Category | Current MB/day | Recompact factor | After MB/day |
|---|---|---|---|
| pm_orderbook | 201.3 | ÷12 | **16.8** |
| kalshi orderbook | 56.3 | ÷12 | **4.7** |
| Other | 9.7 | ÷3 (less repetitive) | **3.2** |
| **Total** | | | **~25 MB/day** |

**Savings ~91%** on steady-state (closed days compact; current day still per-line until rollover).

**Emission cross-check:** `read_jsonl_gz` already iterates gzip members; extend to single-stream gzip or add `read_jsonl_zst`. Query logic unchanged.

**Risk:** Compaction job must be atomic (write temp → rename); open day stays append-only.

### Option C: Retention policy (30d full raw → lossless columnar archive)

**Mechanism:**

- **0–30 days:** current JSONL.gz (or Option A/B).
- **>30 days:** lossless columnar (Parquet): columns `ts_utc`, `endpoint`, `http_status`, `latency_ms`, `payload_json` (or blob). No semantic loss.

Columnar helps **metadata scans**; verbatim JSON blob column does not shrink much (**~0–20%** vs gzip JSONL unless payload dedup applied).

Example stack: Option A live + Option B nightly + 30d retention:

```text
Steady state daily append ≈ 46 MB/day (Option A)
Closed-day recompact ÷10 on rolling 30d corpus ≈ additional marginal savings on disk inventory,
but write rate stays ~46 MB/day.

Without Option A: 267 MB/day × 30d = 8 GB rolling → after recompact ~0.8 GB inventory
```

**Emission cross-check:** For dates in window, same JSONL path. For archived dates, adapter reads Parquet rows and yields identical dict records to `load_logger_books`.

**Savings on disk inventory:** High for old days; **write rate** only reduced if combined with A or B.

---

## 6. Recommendations (ordered)

1. **Implement store-on-change for `pm_orderbook` and `orderbook`** (Option A) — largest lever; aligns with carry-forward analysis semantics; **~83% write reduction** (~46 MB/day).
2. **Add nightly recompact for closed days** (Option B2) — fixes per-line gzip overhead without changing capture; stacks with A for **~5–10 MB/day** steady inventory growth.
3. **Defer zstd alone** (Option B1) — marginal unless paired with batching.
4. **Retention + Parquet** (Option C) — operational relief after 30d; pair with A/B; not sufficient alone.
5. **Optional:** Reduce Polymarket poll frequency for far-from-mid brackets (capture policy change — **out of scope** for 7d; would need separate gate).

---

## 7. VPS operations

```bash
# Storage breakdown + redundancy (read-only)
python scripts/measure_logger_storage.py --data-dir data/raw

# Heartbeat metadata (not raw bytes, but confirms cadence)
python -m ingestion.kalshi_logger --status
python -m ingestion.polymarket_logger --status
sqlite3 data/heartbeat.sqlite "SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM poll_attempts;"
sqlite3 data/polymarket_heartbeat.sqlite "SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM poll_attempts;"
```

---

## 8. Assumptions and limitations

| Assumption | Source |
|---|---|
| 12 Kalshi open markets | Live API 2026-08-22 |
| 22 Polymarket brackets (2 horizon days) | Live Gamma discovery |
| Payload-identical rate 90% for projections | Live 0.5s probe (79–84%) + slower cadence |
| VPS 260 MB/day | User context; model 267 MB/day |
| `orderbook_depth: 0` | config.yaml — API still returns ~1 KB books |
| No NBM/cli/asos in logger write rate | Separate ingestion paths |

**Not measured locally:** multi-day VPS redundancy distribution, trade-heavy days, settlement spikes, disk used by `heartbeat.sqlite` (~small vs raw).

---

## Appendix: measurement reproduction

Live calibration script (run from repo root with network):

```bash
python3 -c "
# See session 7d agent log — combines RawJsonlWriter + live API sizing
# Output: per-category gzip bytes and 267 MB/day projection
"
```

Synthetic gzip overhead: `tests/test_writer.py` + local `RawJsonlWriter` benchmarks in this audit.
