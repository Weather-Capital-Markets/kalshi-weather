# venue-facts.md — Kalshi venue mechanics (settlement, fees, API)

**Status:** PARTIAL — §1 populated from live and historical API payloads; the rest is
placeholder.
**Owner:** the **venue research lane**. Entries below are contributed observations, not
lane-ratified facts; the venue lane ratifies, amends, or rejects each one and owns this file.
**Scope:** venue matters — settlement rules, fees, API endpoint specs, collateral netting.
This file supersedes `data-sources.md` on venue matters.

## Provenance legend

Same tags as `knowledge/data-sources.md`:

| Tag | Meaning |
|---|---|
| `[V-LOCAL]` | Verified by our own reproducible commands; raw output retained. |
| `[V-PRIMARY]` | Read directly from a primary venue document (rulebook, contract text). |
| `[CORR]` | Corroborated: ≥2 independent runs agree; not reproduced end-to-end by us. |
| `[REPORTED]` | Single-instrument claim; plausible, unconfirmed. |

Every entry carries its provenance tag, the command that produced it, and the capture date.

---

## 1. Observed from API payloads

§1.1–§1.5 come from one command, captured while verifying the live-tier schema:

```bash
python -m ingestion.kalshi_history --probe    # 2026-08-14T14:54Z
```

Source market: `KXHIGHNY-26AUG12-T90` (event `KXHIGHNY-26AUG12`), status `finalized`, from
`GET /markets?series_ticker=KXHIGHNY&status=settled`.

§1.6 comes from `--dry-run` (2026-08-14); §1.7–§1.9 from
`--probe --ticker HIGHNY-24AUG15-T83` (2026-08-14), which exercised the historical tier.

Raw JSON for each was pasted in full to the session-2 chat. These are verbatim payload
fields, not recall or documentation.

### 1.1 Last trading minute aligns to the LST climate-day end

`[V-LOCAL]` — `--probe` payload, 2026-08-14.

```
"close_time": "2026-08-13T04:59:00Z"
```

04:59:00Z is 11:59 PM **EST** — one minute before the 05:00Z LST climate-day boundary, on a
date when New York civil time was EDT (00:59 AM EDT). Kalshi's last trading minute therefore
tracks local *standard* time year-round, not civil midnight.

Two consequences:

- Independent corroboration of the fixed-offset UTC−5 climate day in `data-sources.md` §1.1,
  from the venue side rather than the NWS side.
- The spread-census anchor **T = next LST midnight (05:00Z)** sits inside the venue's own
  trading window at every horizon measured. The tightest horizon, T−1h = 04:00Z, is an hour
  before close — a real tradeable moment, not a post-close artifact.

`(verify)` — single market, single date. Confirm the 04:59Z close holds across a winter (EST)
market-day before treating the EST alignment as year-round.

### 1.2 Settlement lands on the first 7/8 AM ET report

`[V-LOCAL]` payload fields + `[V-PRIMARY]` contract text, both 2026-08-14.

```
"settlement_ts":            "2026-08-13T12:04:54.401029Z"
"expected_expiration_time": "2026-08-13T14:00:00Z"
"expiration_time":          "2026-08-19T14:00:00Z"
"settlement_timer_seconds": 3600
"early_close_condition":    "... Expiration will occur on the sooner of the first 7:00 or
                             8:00 AM ET following the release of the data for August 12,
                             2026, or one week after August 12, 2026."
```

12:04:54Z is 8:04 AM ET, consistent with the stated settle-on-first-7-or-8-AM rule. Note
`expiration_time` is the one-week backstop (2026-08-19) while `expected_expiration_time` is
the same-day 14:00Z expectation; actual settlement preceded both.

### 1.3 Settlement source is the NWS Climatological Report (Daily), named verbatim

`[V-PRIMARY]` — contract `rules_primary`, retrieved 2026-08-14.

```
"rules_primary": "If the highest temperature recorded in Central Park, New York for
                  August 12, 2026 as reported by the National Weather Service's
                  Climatological Report (Daily), is greater than 90°, then the market
                  resolves to Yes."
```

The venue names our label source explicitly. CLINYC (`CDUS41 KOKX`) is the correct product,
established from contract text rather than inference.

`rules_secondary` adds that "Preliminary NWS reporting and measurement methods may be subject
to underlying rounding and conversion nuances" — the venue conceding the
intermediate-vs-final issuance distinction that `cli_labels.py` tracks via
`is_same_day_intermediate`.

### 1.4 An empty book is rendered as extreme quotes, not as nulls

`[V-LOCAL]` — candlestick payload from the same `--probe` run, 2026-08-14.

```json
{
  "end_period_ts": 1786456860,
  "volume_fp": "951.00",
  "yes_bid": { "close_dollars": "0.0000" },
  "yes_ask": { "close_dollars": "1.0000" }
}
```

A book with no resting orders reports bid 0.00 / ask 1.00 rather than omitting the fields.
Presence-checking alone would score this as a 99-cent spread instead of "no market". The
census therefore requires bid ≥ $0.01 and ask ≤ $0.99 to call a snapshot two-sided; a 0.00
bid is absence, not a price.

Quiet minutes at zero volume still carry bid/ask closes, so quote coverage does not depend on
trades occurring.

### 1.5 Price and quantity field naming (live tier)

`[V-LOCAL]` — `--probe` payload, 2026-08-14.

Money fields are `*_dollars` **strings** nested under `yes_bid` / `yes_ask` / `price`;
quantities are `*_fp` strings (`volume_fp`, `open_interest_fp`, `yes_bid_size_fp`). Candle
timestamps are `end_period_ts`, epoch seconds.

```
"price_level_structure": "linear_cent"
"price_ranges": [{ "start": "0.0000", "end": "1.0000", "step": "0.0100" }]
```

One-cent tick across the full 0–1 range.

### 1.6 Historical tier routing and series rename

`[V-LOCAL]` — `python -m ingestion.kalshi_history --dry-run`, 2026-08-14.

- `GET /historical/cutoff` → `market_settled_ts = 2026-06-14T00:00:00Z`. Markets settled
  before that route to the historical tier: **8,998 of 9,364 (96%)**.
- The ~2024 series rename is a **ticker-prefix change the historical endpoint spans
  transparently**: `GET /historical/markets?series_ticker=KXHIGHNY` returns 3,948 `KXHIGHNY-*`
  and 5,416 legacy `HIGHNY-*` tickers in one enumeration. `GET /markets?series_ticker=HIGHNY`
  returns zero — the legacy name is not a separately queryable live series.
- Market history spans 2021-08-05 → 2026-08-13; mean open duration ≈ 38 h.

### 1.7 Historical tier uses flat field names, and emits candles only on change

`[V-LOCAL]` — `python -m ingestion.kalshi_history --probe --ticker HIGHNY-24AUG15-T83`,
2026-08-14. Endpoint `/historical/markets/HIGHNY-24AUG15-T83/candlesticks` returned 200.

**Field naming differs from the live tier (§1.5).** The historical tier uses flat
`close`/`high`/`low`/`open`, `volume`, `open_interest` — no `_dollars`, no `_fp`. Values are
still strings; `end_period_ts` is unchanged. `price.*` is `null` for periods without trades.

```json
{
  "end_period_ts": 1723644060,
  "open_interest": "0.00",
  "price": { "close": null, "high": null, "low": null, "mean": null, "open": null,
             "previous": null },
  "volume": "0.00",
  "yes_ask": { "close": "0.9800", "high": "0.9900", "low": "0.9800", "open": "0.9900" },
  "yes_bid": { "close": "0.0100", "high": "0.0100", "low": "0.0000", "open": "0.0000" }
}
```

**Candles are sparse, not a dense 1-minute series.** With `period_interval=1` over a
24-hour window from market open, the endpoint returned **5 candles spanning 11.7 h**, with
inter-candle gaps of 600 s, 1,680 s, 1,800 s, and 38,040 s — 3 of 4 gaps exceed 15 minutes.
Two of five candles had non-zero volume; the others recorded quote changes only. The endpoint
appears to emit a candle when the book or price changed, and to omit unchanged periods.

Two consequences, both measurement-critical:

- A 15-minute staleness rule designed for dense live candles will discard most historical
  snapshots even though the quote it discards was the live book. `spread_census.py` therefore
  reports both the 15-minute-fresh statistics and carry-forward statistics with a quote-age
  distribution; which one is load-bearing is a ratification decision.
- Volume estimates built on "one candle per open minute" are large overestimates for this
  tier. The 2026-08-14 dry-run projected ~21.3 M candles / 9.94 GB assuming density; the
  realized historical-tier footprint should be far smaller.

`(verify)` — one market, and a nearly dead one (`volume_fp` 63.00 lifetime). Candle density
plausibly scales with activity; confirm against a liquid market once bulk data exists.

### 1.8 Last trading time changed convention between 2024 and 2026

`[V-LOCAL]` payloads (2026-08-14) + `[V-PRIMARY]` contract text.

| Market | `close_time` | Local equivalent | vs LST day end (05:00Z) |
|---|---|---|---|
| `HIGHNY-24AUG15-T83` | `2024-08-16T03:59:00Z` | 11:59 PM **EDT** (civil ET) | 61 min **before** |
| `KXHIGHNY-26AUG12-T90` | `2026-08-13T04:59:00Z` | 11:59 PM **EST** | 1 min before |

Both contracts *say* "11:59 PM ET", but the 2024 timestamp is civil-ET midnight while the
2026 timestamp is LST midnight. The effective last trading minute moved one hour later, in
UTC terms, between the two eras.

Census consequence: for pre-change markets the **T−1h snapshot (04:00Z) falls after close**,
so that column is structurally empty for the older era — the same class of artifact as T−48h
predating market open. `spread_census.py` records `in_trading_window` per snapshot and reports
`outside_trading_window_share` so "shut" is never read as "unquoted".

Settlement timing also differs: the 2024 contract expires on "the first 10:00 AM following
the release of the data", the 2026 contract on "the first 7:00 or 8:00 AM ET" (§1.2).

`(verify)` — two markets, two dates. The changeover date is unknown and is computable from
`close_time` across the persisted market index once bulk enumeration lands; do that before
any era-spanning liquidity comparison.

### 1.9 Legacy tickers are not resolvable via the live single-market endpoint

`[V-LOCAL]` — same probe run, 2026-08-14.

`GET /markets/HIGHNY-24AUG15-T83` returns **404** `{"error":{"code":"not_found"}}` even though
the market exists and is returned by `/historical/markets`. Single-market lookups must go
through the historical enumeration for pre-cutoff tickers.

Note the historical market object also lacks the live tier's `floor_strike`-style framing for
this contract (it carries `cap_strike: 83`, `strike_type: "less"`), and `expiration_value` is
an empty string rather than a number.

---

## 2. Fees, collateral, order types

**PLACEHOLDER** — venue lane. Nothing observed yet; do not fill from recall.
