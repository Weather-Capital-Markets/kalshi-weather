# venue-facts.md — Kalshi venue mechanics (settlement, fees, API)

**Status:** PARTIAL — §1 populated from Kalshi backfill; §3 from live Polymarket Gamma/CLOB
probe (2026-08-18); §2.1 fees verified 2026-08-15.
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

§1.6 comes from `--dry-run` (2026-08-14); §1.8–§1.9 from
`--probe --ticker HIGHNY-24AUG15-T83` (2026-08-14), which exercised the historical tier.

Raw JSON for each was pasted in full to the session-2 chat. These are verbatim payload
fields, not recall or documentation.

§1.1, §1.7, §1.8, §1.10 and §1.11 were subsequently revised or established from the
**completed backfill** — all 9,364 markets and 5.25 M candles, 2026-08-14 — via
`python -m analysis.venue_eras` and `python -m analysis.validate_candles`. Where a
single-market probe reading disagreed with the population, the population wins and the
superseded reading is called out in place.

### 1.1 Last trading minute aligns to the LST climate-day end — from 2026-03-18 only

`[V-LOCAL]` — `--probe` payload, 2026-08-14; era bounds from
`python -m analysis.venue_eras` over the full 9,364-market index, 2026-08-14.

```
"close_time": "2026-08-13T04:59:00Z"
```

04:59:00Z is 11:59 PM **EST** — one minute before the 05:00Z LST climate-day boundary, on a
date when New York civil time was EDT (00:59 AM EDT).

The earlier `(verify)` is **resolved, and the original claim was too broad**. LST alignment is
not year-round behaviour; it is the current era only. Across all 1,829 climate days the close
sits 61 minutes before the LST day end on 1,054 of them and 1 minute before on 774, and that
swing is daylight saving rather than a venue decision: a close pinned to 11:59 PM *civil* ET
lands 61 minutes early under EDT and 1 minute early under EST. Testing on EDT days only —
where the two candidate rules name different instants — gives one changeover across five
years, **between climate days 2026-03-17 and 2026-03-18** (1,049 identifying days before,
148 after). See §1.8.

Two consequences:

- Independent corroboration of the fixed-offset UTC−5 climate day in `data-sources.md` §1.1,
  from the venue side rather than the NWS side — but only for the post-2026-03-18 era.
- The spread-census anchor **T = next LST midnight (05:00Z)** sits inside the venue's trading
  window at every horizon **only in the current era**. Before 2026-03-18, T−1h = 04:00Z falls
  after the 03:59Z close on every EDT day, which is 1,054 of 1,829 climate days (58%). The
  census reports `outside_trading_window_share` so that column is never misread as illiquidity.

### 1.2 Settlement lands on the first 7/8 AM ET report — current era only

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

This wording holds from 2024-09-04 onward only; earlier eras name 10:00 AM or nothing at all.
See §1.10 before applying it to a historical market day.

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

**Both tiers emit candles on change; sparseness is a property of quiet markets, not of the
historical tier.** The `(verify)` on this entry is now **resolved, and the original reading
was wrong.** The probe market was nearly dead (`volume_fp` 63.00 lifetime) and returned
5 candles spanning 11.7 h, which was read as a tier property. Across the completed backfill —
5.25 M candles over all 9,364 markets — the two tiers are almost indistinguishable:

| tier | markets | candles | modal gap | median gap | p90 gap | gaps > 1 min | gaps > 15 min |
|---|---|---|---|---|---|---|---|
| live (`/series/.../candlesticks`) | 366 | 430,195 | 60 s | 60 s | 180 s | 22.8% | 1.04% |
| historical (`/historical/...`) | 8,998 | 4,820,806 | 60 s | 60 s | 240 s | 22.1% | 3.07% |

`[V-LOCAL]` — `python -m analysis.validate_candles`, 2026-08-14, after the full backfill.

The live tier skips 22.8% of its own minute boundaries, so it is **not** a dense
one-candle-per-minute series either, and cannot serve as a dense control. The historical tier
is modestly quieter (higher p90, 3× the share of >15-minute gaps), which is what an older and
less liquid market population predicts — not a different emission mechanism.

Three consequences, all measurement-critical:

- A 15-minute staleness rule discards live books on **both** tiers. The count of long gaps is
  small but their time coverage is not: at T−6h on the live tier, 24.6% of in-window snapshots
  have a last quote older than 15 minutes even though 99% of gaps are under it. Carry-forward
  is the ratified primary statistic in `spread_census.py`, with the 15-minute rule retained as
  `_strict15` robustness.
- Volume estimates built on "one candle per open minute" are large overestimates. The
  2026-08-14 dry-run projected ~21.3 M candles / 9.94 GB; realized is **5.25 M candles and
  87 MB on disk**, 0.9% of the projection. Density and per-candle bytes both came in low.
- Emission-on-change is safe for carry-forward only if omitted periods carry no trades. That
  holds for 9,317 of 9,364 markets exactly; see §1.11 for the 47 that do not.

### 1.8 Last trading time changed once, between climate days 2026-03-17 and 2026-03-18

`[V-LOCAL]` — `python -m analysis.venue_eras` over all 9,364 enumerated markets
(1,829 climate days, 2021-08-06 → 2026-08-12), 2026-08-14. Supersedes the earlier
two-market reading of this entry, which put the change "between 2024 and 2026".

Measured naively, the offset from the LST climate-day end flips twice a year and would
suggest thirteen changeovers:

| `close_time` offset | climate days | first | last |
|---|---|---|---|
| 61 min before day end | 1,054 | 2021-08-06 | 2026-03-17 |
| 1 min before day end | 774 | 2021-11-07 | 2026-08-12 |
| 721 min before day end | 1 | 2021-11-27 | 2021-11-27 |

Every one of those flips lands on a daylight-saving boundary. A close pinned to 11:59 PM
civil ET *is* 61 minutes before the LST day end under EDT and 1 minute before it under EST,
so the seasonal swing is the null hypothesis, not a finding. **Under EST the two candidate
rules name the same instant and identify nothing**; only EDT days carry information. Excluding
the 10 daylight-saving transition climate days — where the venue's own close sits an hour from
both rules (2021-11-07, 2022-03-13, 2022-11-06, 2023-03-12, 2023-11-05, 2024-03-10,
2024-11-03, 2025-03-09, 2025-11-02, 2026-03-08) — leaves one change across 1,197 identifying
days:

| last-trading-time rule | climate days | first | last |
|---|---|---|---|
| 11:59 PM civil ET | 1,049 | 2021-08-06 | 2026-03-17 |
| 11:59 PM LST (fixed 04:59Z) | 148 | 2026-03-18 | 2026-08-12 |
| both (EST, non-identifying) | 621 | 2021-11-08 | 2026-03-07 |
| other (11:59 LST) | 1 | 2021-11-27 | 2021-11-27 |

**The contract text did not follow the timestamp.** `early_close_condition` still reads
"The Last Trading Time will be 11:59 PM ET" on post-change markets whose `close_time` is
04:59Z — 12:59 AM EDT, an hour after the stated time. `[V-PRIMARY]` (`KXHIGHNY-26AUG12-T90`
text) against `[V-LOCAL]` (its own `close_time`). Trust the timestamp, not the prose, and do
not re-derive the last trading minute from contract wording.

Census consequence: for pre-2026-03-18 EDT markets the **T−1h snapshot (04:00Z) falls after
close** — 1,054 of 1,829 climate days — so that column is structurally empty there, the same
class of artifact as T−48h predating market open. `spread_census.py` records
`in_trading_window` per snapshot and reports `outside_trading_window_share` so "shut" is never
read as "unquoted".

### 1.9 Legacy tickers are not resolvable via the live single-market endpoint

`[V-LOCAL]` — same probe run, 2026-08-14.

`GET /markets/HIGHNY-24AUG15-T83` returns **404** `{"error":{"code":"not_found"}}` even though
the market exists and is returned by `/historical/markets`. Single-market lookups must go
through the historical enumeration for pre-cutoff tickers.

Note the historical market object also lacks the live tier's `floor_strike`-style framing for
this contract (it carries `cap_strike: 83`, `strike_type: "less"`), and `expiration_value` is
an empty string rather than a number.

### 1.10 Settlement-time regime history

`[V-LOCAL]` + `[V-PRIMARY]` — `python -m analysis.venue_eras`, contract text of all 9,364
markets, 2026-08-14.

The snapshot after which expiration occurs is stated in `early_close_condition` /
`rules_secondary` and moved twice:

| contract phrase | climate days | first | last |
|---|---|---|---|
| unspecified (Rule 100.19 reference only) | 141 | 2021-08-06 | 2021-12-25 |
| "the first 10:00 AM following the release of the data" | 980 | 2021-12-28 | 2024-09-03 |
| "the first 7:00 or 8:00 AM ET following the release of the data" | 708 | 2024-09-04 | 2026-08-12 |

Changeovers: between climate days **2021-12-25 and 2021-12-28** (no market days in the gap),
and between **2024-09-03 and 2024-09-04**. §1.2's 7/8 AM finding is therefore the current era
only, not a property of the series.

**Unspecified era (2021-08-06 → 2021-12-25):** contract text defers to Rulebook Rule 100.19
with no snapshot hour named. Until Rule 100.19 is read from the archived 2021 PDF (open item
V1), label selection for those 141 climate days **defaults to the first 10:00 AM rule**, tagged
as an assumption — not a ratified fact.

Note the legacy wording omits "ET"; a regex requiring it silently reclassifies the entire
10 AM era as unspecified.

This is measurement-critical for labels, not just trivia: a 10 AM snapshot can see a CLINYC
revision that a 7/8 AM snapshot cannot. `data-sources.md` §1.3 defines the label as the latest
issuance visible at the settlement snapshot, so the snapshot time is era-dependent and the
label rule cannot be applied with a single fixed hour across the archive.

### 1.11 Candle volume does not always reconcile to the market's lifetime volume

`[V-LOCAL]` — `python -m analysis.validate_candles` over the completed backfill, 2026-08-14.

Summing every candle's `volume` for a market should reproduce that market's `volume_fp`. It
does exactly for **9,317 of 9,364 markets**. The 47 that miss account for 7,225 of
138,212,600.30 contracts — **0.0052%** — spread over 34 of 1,829 climate days.

The residual is **not** simply missing capture:

- 40 markets fall short of the lifetime volume, but **7 exceed it**. A candle sum above the
  market total cannot be produced by omitting candles, so at least part of this is venue-side
  bookkeeping disagreement between two fields.
- Re-fetching the single live-tier mismatch (`KXHIGHNY-26JUN18-T83`, short 15.00 of 54,302.97)
  over a window widened by a day on each side returned a byte-identical candle set. The gap is
  in the venue's data, not in our chunking.
- Mismatches cluster on a few dates (2025-03-10 accounts for 6 markets and 2,788 contracts),
  which points at venue incidents rather than a systematic tier property.

Consequence: the emission-validation gate as pre-registered ("any mismatch fails") **fails**
on exact equality. K1 v3 (see `knowledge/plan.md` §3) accepts capture at 9,317/9,364 exact
(0.0052% residual volume, 7 over-reconciliations inconsistent with capture loss) and treats
the residual as venue bookkeeping. Primary census includes all markets; robustness column
`_exclnoreconcile` excludes the 47 markets. Quote-only emission completeness is unverifiable
for history; a forward VPS cross-check is a standing obligation that caveats prospective use
but does not retroactively void a K1 verdict.

### 1.12 Forward logger–candle match: unreproducible prose figures (2026-09-13)

`[V-LOCAL]` — audit of Stage C1-M1 Stage 0 artifact, 2026-09-13.

Project prose has long cited **0.9% logger–candle match** and **2,026 silent book changes**
at convention `interval_start|UTC|+0` as the standing obligation that voids reconstruction
claims and caveats emit-on-change. Under audit those figures are **unreproducible**:

| Claim | Status |
|---|---|
| `match_rate = 0.009` | Transcribed from project-record prose only. No named input files, no hashes, no code path, **no recorded window**. |
| `silent_count = 2026` | Same: transcribed prose only. |
| `n = 225,111` boundaries | **Confabulated.** Never appeared in the project record. Manufactured as `round(2026 / 0.009)`, treating the silent-change count as if it were a match count. Those quantities are near-complements; the formula is wrong twice. Withdrawn. |
| Window for the 0.9% cross-check | `UNKNOWN_NOT_IN_PROJECT_RECORD`. Do **not** recover it. |

**Policy going forward.** Abandon window archaeology. Run all twelve join conventions —
including `interval_start|UTC|+0` — on one **declared** window (suggested start
`2026-08-19` through the latest complete climate day on the VPS logger). The freshly
computed baseline row supersedes the transcribed one; transcribed figures remain only in
the inputs manifest as historical provenance. Decision bands and the well-posedness gate
are preregistered in `prereg/c1-m1-v1-stage0-decision.yaml` and
`analysis/emission_decision.py` **before** the VPS sweep runs. Intermediate
(`0.10 ≤ max < 0.60`) rates are `PARTIAL`: no auto-branch, no averaging; the sweep
writes `winner_match_distribution` by market and by day for written adjudication.
`max < 0.10` is `LOW` and requires the well-posedness hand audit before any
falsification claim.

**Hypothesis to check before reading twelve low rows as falsification.** Candles may be
trade-derived while the logger records quote state. If so, no join convention can align
them, ~0.9% is the expected result rather than a defect, and the correct finding is
`CROSS_CHECK_ILL_POSED` (redesign required) — not emit-on-change falsified. Those have
different consequences for the historical corpus: falsified ⇒ books degraded; ill-posed ⇒
books merely unvalidated by this test. See `analysis/emission_wellposedness.py`.

### 1.13 Trade record identifies the aggressor (corrected 2026-09-13)

`[V-PRIMARY]` — re-fetch of `GET /markets/trades` and `GET /historical/trades` docs,
2026-09-13. See `knowledge/06-kalshi-trades-api.md`.

Both endpoints carry `taker_outcome_side` (`yes`/`no`) and `taker_book_side`
(`bid`/`ask`), marked required. The deprecated `taker_side` field is not to be read.
The 2026-08-14 extraction omitted these fields. **An extracted schema is a claim about
a page at a point in time, not about the API.** Same failure class as the 441-minute
NBM latency and the orphan 0.9%.

Consequence: maker/taker attribution on `KXHIGHNY` runs on the full trade history with
no book and no Lee-Ready classifier. C1-X1 (`prereg/c1-x1-v1.yaml`) is the study.
The public object still has **no user, firm, or maker id**. Maker *side* is known;
maker *identity* is not. Participant concentration on this tape is not inferable.

### 1.14 Fractional `count_fp` is contract size, from 2026-04-09 on this tape

`[V-PRIMARY]` — Kalshi docs, [Fixed-Point Representation](https://docs.kalshi.com/getting_started/fixed_point_migration),
last updated 2026-08-20. `[V-LOCAL]` — C1-M2 part 2 on the labelled six-bracket
KXHIGHNY parquet (`analysis/out/c1_m2_era/`).

`count_fp` is a fixed-point string, two decimal places, minimum **0.01 contracts**.
Fractional fills appear even when the client does not place fractional orders. That
is economic size, not a recoding of the same integer contract. Internally multiplying
by 100 to get integer centi-contracts is bookkeeping; retention stays cents per
contract on the `Decimal` count.

On this tape, non-integer `count_fp` first appears on climate day **2026-04-09** and
runs through **2026-09-13** in this corpus: 420,226 prints, 11.26% of premium
(denominator: fractional plus integer labelled non-block premium). All of it sits
after the MM Program's scheduled end. C1-M2 part 2 includes those prints as `Decimal`
size. None were unattributed. Do not floor, round, or drop them. Fee-schedule
attribution (`attribute_trade`) still refuses non-integers; that path is a different
lane.

### 1.15 Short-horizon mark-outs understate adverse selection on low-volume days

`[V-LOCAL]` — C1-M2 part 2 common subset (n = 2,233,073 fills / 1,340 days),
horizon × daily-volume decile grid. Days ranked on full-universe daily premium.
Retention in cents per contract; both weightings; day-clustered 95% CI.

On the quietest tenth of days (decile 1), the 30-minute realised half-spread is
**+4.734¢** trade-weighted [4.204, 5.345] and **+5.294¢** day-weighted. Settlement
retention on the same fills is **−2.894¢** trade-weighted [−4.927, −0.635] and
**−3.178¢** day-weighted [−6.436, +0.039]. One-minute and five-minute marks on that
decile are also positive (~5.4–6.0¢). Quiet days look fine at half an hour and are
negative by resolution on the trade-weighted measure.

On the busiest tenth (decile 10), settlement stays positive: **+1.951¢**
trade-weighted [1.462, 2.434], **+2.249¢** day-weighted [1.637, 2.826], above the
30-minute mark on that decile.

A quoting policy calibrated on 30-minute marks would be systematically wrong about
the days where retention is actually negative.

---

## 1.99 Open items (venue lane)

| # | Item | Blocking? | Resolution path |
|---|---|---|---|
| V1 | Settlement-time regime history: the 2021-08-06 → 2021-12-25 era (141 climate days) names no snapshot time, deferring to Rulebook Rule 100.19. Those days cannot have `data-sources.md` §1.3 applied from contract text alone. | Before labelling 2021 market days | Read Rule 100.19 as it stood in 2021 from the archived rulebook PDF |
| V2 | The contract prose ("11:59 PM ET") contradicts `close_time` (04:59Z) after 2026-03-18 (§1.8). Whether the venue changed policy or has a stale template is unknown. | No — the timestamp is authoritative for measurement | Venue support, or watch whether the prose catches up |
| V3 | 47 markets across 34 climate days whose candle volume does not reconcile to `volume_fp`, in both directions (§1.11). 0.0052% of total volume. K1 v3: primary includes all days; robustness column `_exclnoreconcile` drops these 47 markets; kill-direction disagreement → written discussion. Venue-lane bookkeeping explanation, not a census blocker. | No | Explain via venue support if needed; forward VPS quote check is the standing obligation for quote-only emission |
| V4 | Weather-series maker fee for KXHIGHNY / KXHIGH* | No — **closed 2026-08-15** | §2.1; re-check before any capital commitment |

---

## 2. Fees, collateral, order types

### 2.1 Weather-series trading fees (KXHIGHNY)

`[V-LOCAL]` — Kalshi live fee schedule page, 2026-08-15 (screenshot retained).

- **KXHIGHNY** and no **KXHIGH\*** series appear in the venue's 161-series non-standard fee
  table. The page states **"No upcoming fee changes scheduled."**
- **Weather maker fee: $0.00** — upgraded from inference to verified against the live fee
  list. Both API-tier PDFs and the live page now agree.
- **Census readout:** a 2–4¢ spread result can be read as **maker-only live** without an
  asterisk on fees, subject to the re-check obligation below.

**Re-check before any capital commitment** — fee schedules are time-sensitive (see project file
02's time-sensitivity note). The live list must be re-verified before deploying capital, even
though no change is scheduled today.

### 2.2 Collateral, order types

**PLACEHOLDER** — venue lane. Not yet observed from primary sources.

---

## 3. Polymarket NYC daily-high (Gamma `nyc-daily-weather`)

`[V-LOCAL]` — `python -m ingestion.polymarket_logger --probe` and `--once` against live Gamma +
CLOB, 2026-08-18. Supersedes any earlier gateway-probe claim that Polymarket settled on the same
NWS Central Park report as Kalshi.

### 3.1 Market structure — exhaustive disjoint bracket ladder

`[V-LOCAL]` + `[V-PRIMARY]` — Gamma event payloads (`groupItemTitle`, `question`, `negRisk`).

Polymarket's NYC daily-high series is an **exhaustive bracket ladder**, not a set of cumulative
`≥X` threshold binaries:

- Tails: `≤75°F` (`tail_below`) and `≥94°F` (`tail_above`).
- Interior: disjoint 2°F bins (`between 76-77°F`, `between 78-79°F`, …).
- `negRisk: true` on every open bracket — the venue links them as a **mutually exclusive** set.
- The probability law is **Σp ≈ 1 across the ~11 brackets**, identical to Kalshi's ladder form.
  Monotonicity laws for cumulative thresholds (P(≥87) ≥ P(≥89)) **do not apply**.

Cross-venue S2 comparison is therefore **bracket-to-bracket** (same bin label), not threshold-to-
threshold.

Logger metadata encodes `bracket_kind`, `bracket_low_f`, `bracket_high_f`, `bracket_label`, and
`neg_risk` — not `strike_f` / `direction` cumulative reframing.

### 3.2 Settlement station and data source — KLGA, not KNYC

`[V-PRIMARY]` — Polymarket contract / resolution rules (market questions and resolution criteria
on Gamma); `[CORR]` — station climatology (LaGuardia vs Central Park summer bias).

**Hard finding:** Polymarket resolves NYC daily-high on **LaGuardia (KLGA)** using **Weather
Underground's Daily Observations** table. Kalshi resolves on **Central Park (KNYC)** via the
**NWS Climate Report** (CLINYC). Different station, different data provider, different revision
rule:

| | Polymarket | Kalshi |
|---|---|---|
| Station | KLGA (LaGuardia) | KNYC (Central Park) |
| Source | Weather Underground Daily Observations | NWS Climate Report |
| Revision | Accepts revisions until the next day's first datapoint | Settles on first 7/8 AM ET snapshot; ignores later revisions |

LaGuardia routinely runs **1–3°F warmer** than Central Park in summer — enough to land in a
different 2°F bracket on most warm days.

**Consequence:** cross-venue price differences are **mostly basis** (KLGA–KNYC spread), not
mispricing. Any naive "arbitrage" between venues is a bet on the station spread, not free money.
This **kills naive cross-venue arb** as a thesis.

### 3.3 Order-book depth and fees — materially deeper than Kalshi

`[V-LOCAL]` — CLOB `/book` dumps from `--probe`, 2026-08-18; compared to Kalshi depth census
(~$14 within 2¢ on KXHIGHNY).

Polymarket's resting liquidity on NYC weather brackets is **an order of magnitude deeper** than
Kalshi's observed books:

- ~100 contracts resting at nearly every penny from 3¢ to 35¢ on sample brackets; thousands at
  tails; `liquidityNum` ~$20–38k per bracket; daily volume ~$10–50k per bracket on active days.
- `feeSchedule`: **`takerOnly: true`**, rate **0.05**, **`rebateRate: 0.25`** — taker-only fees
  with a maker rebate.
- `rewardsMinSize` / `rewardsMaxSpread: 4.5` fields indicate a **liquidity-rewards** program.

**Consequence for maker thesis:** shallow Kalshi books may be a **Kalshi-specific** depth
problem, not a weather-market problem. Polymarket appears to have depth, volume, and a maker-
friendly fee structure — with the tradeoff that it settles on a different station (§3.2).
Depth and fee findings should be reported **venue-split** in any census readout.

### 3.99 Open items (Polymarket)

| # | Item | Blocking? | Resolution path |
|---|---|---|---|
| P1 | Exact Polymarket revision cutoff rule in contract text (Wunderground table row selection) | Before label backfill | Read resolution criteria from Gamma / primary contract |
| P2 | Liquidity-rewards economics (maker rebate + rewards program) | Before capital on Polymarket | Fee schedule doc + live account terms |
| P3 | KLGA–KNYC spread distribution by season (basis sizing) | Before cross-venue analysis | ASOS / CLI archive comparison |
