# venue-facts.md — Kalshi venue mechanics (settlement, fees, API)

**Status:** PARTIAL — §1 populated from live API payloads; the rest is placeholder.
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

## 1. Observed from live API payloads

All of §1 comes from one command, captured incidentally while verifying schema:

```bash
python -m ingestion.kalshi_history --probe    # 2026-08-14T14:54Z
```

Source market: `KXHIGHNY-26AUG12-T90` (event `KXHIGHNY-26AUG12`), status `finalized`,
from `GET /markets?series_ticker=KXHIGHNY&status=settled`. Raw JSON was pasted in full to
the session-2 chat. These are verbatim payload fields, not recall or documentation.

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

# TODO(verify): `/historical/markets/{ticker}/candlesticks` has not yet returned a payload,
# so historical-tier field naming is unconfirmed and may differ from §1.5.

---

## 2. Fees, collateral, order types

**PLACEHOLDER** — venue lane. Nothing observed yet; do not fill from recall.
