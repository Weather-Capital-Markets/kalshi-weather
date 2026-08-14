# venue-facts.md — Kalshi venue mechanics (settlement, fees, API)

**Status:** PLACEHOLDER for the venue research lane, with one populated section.

This file will supersede `data-sources.md` on venue matters (settlement rules, fees,
API endpoint specs, collateral netting). Referenced by `knowledge/data-sources.md`.

## 1. Observed from live API payloads (primary source)

Captured incidentally by `python -m ingestion.kalshi_history --probe` on
2026-08-14T14:54Z. These are verbatim fields from a real market object, not
recall or documentation. **For the venue lane to formalize.**

Source market: `KXHIGHNY-26AUG12-T90` (event `KXHIGHNY-26AUG12`), status
`finalized`, from `GET /markets?series_ticker=KXHIGHNY&status=settled`.

### 1.1 Last trading minute aligns to the LST climate-day end

```
"close_time": "2026-08-13T04:59:00Z"
```

04:59:00Z is 11:59 PM **EST** — one minute before the 05:00Z LST climate-day
boundary, on a date when New York civil time was EDT (00:59 AM EDT). Kalshi's
last trading minute therefore tracks local *standard* time year-round, not
civil midnight.

Two consequences:

- Independent corroboration of the fixed-offset UTC−5 climate day in
  `data-sources.md` §1.1, from the venue side rather than the NWS side.
- The spread census anchor **T = next LST midnight (05:00Z)** is inside the
  venue's own trading window at every horizon we measure. The tightest
  horizon, T−1h = 04:00Z, is an hour before close — a real tradeable moment,
  not a post-close artifact.

### 1.2 Settlement lands on the first 7/8 AM ET report

```
"settlement_ts":            "2026-08-13T12:04:54.401029Z"
"expected_expiration_time": "2026-08-13T14:00:00Z"
"expiration_time":          "2026-08-19T14:00:00Z"
"settlement_timer_seconds": 3600
"early_close_condition":    "... Expiration will occur on the sooner of the first
                             7:00 or 8:00 AM ET following the release of the data
                             for August 12, 2026, or one week after August 12, 2026."
```

12:04:54Z is 8:04 AM ET, consistent with the stated settle-on-first-7-or-8-AM
rule. Note `expiration_time` is the one-week backstop (2026-08-19), while
`expected_expiration_time` is the same-day 14:00Z expectation; actual
settlement preceded both.

### 1.3 Settlement source is the NWS Climatological Report (Daily), named verbatim

```
"rules_primary": "If the highest temperature recorded in Central Park, New York
                  for August 12, 2026 as reported by the National Weather
                  Service's Climatological Report (Daily), is greater than 90°,
                  then the market resolves to Yes."
```

The venue names our label source explicitly. CLINYC (`CDUS41 KOKX`) is the
correct product, from the contract text rather than inference.

`rules_secondary` further warns that "Preliminary NWS reporting and measurement
methods may be subject to underlying rounding and conversion nuances" — which
is the venue conceding the intermediate-vs-final issuance distinction that
`cli_labels.py` already tracks via `is_same_day_intermediate`.

### 1.4 An empty book is rendered as extreme quotes, not as nulls

From the candlestick payload for the same market:

```json
{
  "end_period_ts": 1786456860,
  "volume_fp": "951.00",
  "yes_bid": { "close_dollars": "0.0000" },
  "yes_ask": { "close_dollars": "1.0000" }
}
```

A book with no resting orders reports bid 0.00 / ask 1.00 rather than omitting
the fields. Presence-checking alone would score this as a 99-cent spread
instead of "no market". The census therefore requires bid ≥ $0.01 and
ask ≤ $0.99 to call a snapshot two-sided (A6); a 0.00 bid is absence, not a
price.

Quiet minutes at zero volume still carry bid/ask closes, so quote coverage does
not depend on trades occurring.

### 1.5 Price and quantity field naming

Money fields are `*_dollars` **strings** nested under `yes_bid` / `yes_ask` /
`price`; quantities are `*_fp` strings (`volume_fp`, `open_interest_fp`,
`yes_bid_size_fp`). Candle timestamps are `end_period_ts`, epoch seconds.

```
"price_level_structure": "linear_cent"
"price_ranges": [{ "start": "0.0000", "end": "1.0000", "step": "0.0100" }]
```

One-cent tick across the full 0–1 range.

# TODO(verify): all of the above is from a single post-cutoff market served by
# the live path. The `/historical/markets/{ticker}/candlesticks` tier has not
# yet returned a payload, so its field naming is unconfirmed.
