# data-sources.md — Forecast & label data sources for US temperature markets (KNYC first)

**Status:** ACTIVE — source of truth for weather/forecast/label data claims.
**Drafted:** 2026-08-13, from the NBM research lane (empirical verification sessions of 2026-08-11/12).
**Scope:** NBM forecast data, NWS climate-report labels, and their archives. Venue mechanics
(settlement rules, fees, API endpoints) live in `venue-facts.md`, which supersedes anything here on venue matters.
**Supersession rule:** A claim in this file supersedes any claim from chat memory or AI recall.
Anything tagged `(verify)` must be confirmed before load-bearing use.

## Provenance legend

| Tag | Meaning |
|---|---|
| `[V-LOCAL]` | Verified by our own reproducible commands; raw output retained (hashes where noted). |
| `[V-PRIMARY]` | Read directly from a primary NOAA/NWS/IEM document fetched in-session by Claude. |
| `[CORR]` | Corroborated: ≥2 independent tool runs agree, consistent with primary documents; not reproduced end-to-end by us. |
| `[REPORTED]` | Single-instrument claim (one AI research run); plausible, unconfirmed. |

The empirical runs of 2026-08-12 were executed by ChatGPT-orchestrated PowerShell (report with
SHA-256 hashes) and a Perplexity-orchestrated session; where their raw outputs are byte-identical
on the same files, facts are marked `[CORR]` at minimum. Nothing in this file rests on either
tool's *interpretation* — only on quoted raw output cross-checked against primary documents.

---

## 1. Settlement label (the `y` variable)

### 1.1 Definition of the climate-day maximum

- The CLI (Daily Climate Report) covers the calendar day **midnight-to-midnight in Local
  Standard Time, year-round** — during daylight saving time the observation day runs
  **1:00 AM – 1:00 AM local daylight time**. A max at 12:40 AM EDT belongs to the *previous*
  day's climate day. `[CORR]` — NWSI 10-1004 (`weather.gov/media/directives/010_pdfs/pd01010004curr.pdf`,
  dated 2025-06-04) and weather.gov/lot observations FAQ (dated 2026-05-28), both quoted verbatim
  in round-1 research; URLs `[REPORTED]` `(verify: one-click each — O7)`.
- Consequence for label & clock code: the "post-max regime" boundary and day-assignment logic
  must use the LST day, not the civil day. Edge case is real on warm-front nights.

### 1.2 Label source: IEM AFOS archive of CLINYC as-issued

- Product: `CLINYC` (currently `CDUS41 KOKX` "CLIMATE REPORT"), archived as originally
  transmitted (raw NOAAPort text), retrievable per issuance. `[CORR]`
- Endpoints `[V-LOCAL — both runs executed these]`:
  - Single issuance: `https://mesonet.agron.iastate.edu/wx/afos/p.php?pil=CLINYC&e=YYYYMMDDHHMM`
  - Bulk/range: `https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil=CLINYC&fmt={text|zip}&sdate=...&edate=...&limit=N&order={asc|desc}`
- **Archive floor: 1983-05-31 21:00 UTC** (`WOUS00 KNYC 312100`, "SUMMARY OF THE DAY", 5 PM EDT
  Tue May 31 1983). Negative-boundary query ending immediately before that timestamp returns
  `ERROR: Could not Find: CLINYC`. `[CORR]` — identical result in both runs; ZIP member
  `CLINYC_198305312100.txt`.
- Format eras (parser must branch): `WOUS00` "SUMMARY OF THE DAY" 1983–~1985; "DEGREE DAY DATA"
  interlude ~1987; `TTAA00` "CLIMATOLOGICAL SUMMARY" ~1988–1996; `CDUS41 KOKX` "CLIMATE REPORT"
  modern. `[REPORTED]` — **decision: build the modern parser only**; markets exist only from
  ~2021, and pre-2005 coverage is thin (Jan issuance counts: 1996→64, 2000→46, 2010→380,
  2020→335 `[REPORTED]`).
- Multiple issuances per climate day exist (same-day intermediates "VALID TODAY AS OF ...",
  next-morning report, corrections). `[CORR]`
- **`retrieve.py` caps `limit` at 9999** and answers anything larger with HTTP 422 and a
  pydantic validation body — not an empty result. A too-large limit therefore fails every
  month silently unless the status code is checked. `[V-LOCAL]` — 2026-08-14; `ingestion/cli_labels.py`
  pins the limit and warns if a month's product count reaches it.
- **Realized backfill (2020-01 → 2026-08, `python -m ingestion.cli_labels`, 2026-08-14)**
  `[V-LOCAL]`: 4,913 parsed issuances over 2,416 distinct climate dates (2019-12-31 →
  2026-08-13), of which 2,440 are same-day intermediates. Applying the full-day rule leaves
  **2,413 candidate labels**; 25 report `MM` rather than a temperature. Only 2 products in the
  whole range failed the modern-parser check. Against the 1,829 climate days that carry a
  Kalshi market, **1,825 have a full-day label and 4 do not**: 2025-06-02 and 2025-06-03 have
  no CLINYC issuance in the archive at all, while 2025-06-18 and 2025-11-13 have only a
  same-day intermediate and no full-day report. Tracked as O10.
- The TIME column header is `(LST)` on **every** parsed issuance, with no seasonal variation.
  That is the printed label, not proof the values are LST in summer; the ASOS cross-check
  (`spread_census.py` `OPEN_ITEM_LST`) is still required.

### 1.3 Label-selection rule (DECIDED)

> For each market day, the label is taken from the **latest CLINYC issuance visible at Kalshi's
> settlement snapshot time** (first 7:00/8:00 AM ET check per `venue-facts.md`) **that covers the
> full prior climate day**. Later issuances and corrections are logged separately and never
> overwrite the label; the preliminary-vs-final disagreement rate is itself a tracked metric.

Do **not** label from METAR-derived maxima, GHCN-D, or "final" climate data — those can disagree
with what Kalshi settled on. Rationale and settlement mechanics: `venue-facts.md`.

**Snapshot time is era-dependent; see `venue-facts.md` §1.10 for the settlement-time history.**
The rule above is stated with the current 7/8 AM snapshot, but that wording only holds from
2024-09-04. Earlier markets settle on "the first 10:00 AM", and the first 141 climate days
(2021-08-06 → 2021-12-25) name no time in the contract at all. Applying one fixed hour across
the archive would mis-select the issuance for most market days — a 10 AM snapshot can see a
CLINYC revision an 8 AM snapshot cannot. `[V-LOCAL]` — `python -m analysis.venue_eras` over
all 9,364 markets, 2026-08-14. Tracked as O9.

### 1.4 `taker_outcome_side` × `taker_book_side` (trade-derived anchor)

C1-M1 addendum 2 must not assume `taker_book_side`'s frame of reference. The docs call it
only "book side equivalent to `taker_outcome_side`". Direction for the YES-space estimator
uses `taker_outcome_side` alone (`yes` → d=+1 / offer; `no` → d=−1 / bid). The cross-tab
is a gate: it must be a clean one-to-one bijection or the estimator stops.

**Addendum 2's stated pairing was wrong, and the correction matters less than what it
reveals.** Addendum 2 asserted `yes` pairs with `ask`, reasoning that buying YES lifts a YES
offer. The observed table is the opposite, `yes↔bid` / `no↔ask`, so `taker_book_side` is not
in the YES-book frame addendum 2 assumed. That is a bookkeeping correction. The load-bearing
observation is the **off-diagonal being exactly zero**: `taker_book_side` is a deterministic
function of `taker_outcome_side`, carries zero independent information, and therefore
**cannot corroborate the direction sign**. A bijection is a consistency gate, not a second
source. Any code or prose that treats a clean cross-tab as sign verification is wrong.

| Item | Status |
|---|---|
| Observed mapping | **clean anti-diagonal**. Full six-bracket ingest 2026-09-15: `yes → bid` (1,899,558), `no → ask` (1,508,830), `yes×ask` = 0, `no×bid` = 0. Earlier v0-MINIMAL probe the same day (50 markets, n=28,588) agreed: 17,813 / 10,775. Prior 2026-09-13 live sample (68,305 / 47,082) also agreed. |
| Sample (full ingest) | Cutoff-first public pull: `GET /historical/cutoff` (`market_settled_ts=2026-07-16T00:00:00Z`), then `/historical/trades` + `/markets/trades` for every KXHIGHNY/HIGHNY ticker from 2022-12-11. n=3,405,388 non-block prints, 8,241 markets, 0 errors, 0 complement failures. `python -m analysis.v0_ingest`; `python -m analysis.v0_measure`. |
| Sample (v0-MINIMAL probe) | `python -m analysis.probe_taker_mapping --series KXHIGHNY --max-markets 50 --limit 1000`. Gate passed before any v0-MINIMAL anchor edit. |
| Gate | `wxmm.fairvalue.anchor_trades.assert_outcome_bookside_mapping` |
| Information content | **None.** Off-diagonal is exactly 0, so `taker_book_side` is redundant with `taker_outcome_side`. It is not a second source and never verifies d. |
| Direction | From `taker_outcome_side` only (`yes` → d=+1 / YES-space ask print; `no` → d=−1 / YES-space bid print). The economics are unambiguous: a taker who bought YES at `yes_price` lifted someone's offer, so the print sets the ask. |
| Probe | `python -m analysis.probe_taker_mapping --series KXHIGHNY` |

### 1.5 Trade-derived two-sided coverage (not S2 reconstructed books)

Share of grid points with **both** YES-space sides valid and uncrossed, hourly
hours-to-close T−24 … T−0, over the 8,241-market six-bracket universe (including
zero-volume tickers, which count as missing). This is not S2's 22.4% complete
two-sided reconstructed books on bracket-days.

`[V-LOCAL]` — `python -m analysis.v0_measure`, 2026-09-15.

| Slice | two-sided uncrossed | n_grid | share |
|---|---:|---:|---:|
| pooled | 138,366 | 206,025 | **0.6716** |
| DJF | 34,189 | 52,425 | 0.6522 |
| MAM | 39,216 | 55,200 | 0.7104 |
| JJA | 38,290 | 55,200 | 0.6937 |
| SON | 26,671 | 43,200 | 0.6174 |

Floor 0.15. Below-floor = false, so the v0 fit is allowed. Also: one-sided 23,458;
missing 18,417; crossed-resolved 25,784 (rate 0.1251).

Complete **ladders** (every contract on the climate day two-sided uncrossed at a
prediction origin) are a stricter cut: 1,303 of 6,770 labelled (day × {24,12,6,3,1}h)
slots. That is the design matrix, not the coverage floor.

### 1.6 C1-M1 v0 sign test (OOS RPS vs the null)

Null is β = 0 = the normalised trade-derived ladder. Labels are CLINYC as-issued
(1,354 unique-winner days; 0 venue-`result` disagreements after inferring the
lower T-suffix as `less` when `strike_type` is missing). NBM =
`UNAVAILABLE_INTERPOLATION_UNSPECIFIED`. Scores, never P&L.

`[V-LOCAL]` — `python -m analysis.v0_score`, 2026-09-15. n=1,300 OOS predictions.
Walk-forward expanding window, `min_train_days=30`, day-clustered percentile CI,
1,000 resamples, seed 0.

| | mean ΔRPS (null − model) | clustered 95% CI | n |
|---|---:|---:|---:|
| pooled | **−0.0401** | **[−0.0529, −0.0291]** | 1,300 |
| DJF | −0.0503 | [−0.0847, −0.0220] | 317 |
| MAM | −0.0351 | [−0.0564, −0.0152] | 374 |
| JJA | −0.0360 | [−0.0511, −0.0202] | 377 |
| SON | −0.0409 | [−0.0539, −0.0278] | 232 |

Contract-level CI [−0.0490, −0.0307] is narrower, as required. Decision rule
`sign_test_oos_rps_improvement_vs_null`: clustered interval excludes zero and is
negative → **`harmful_check_sign`**. Flow+calendar as specified is worse than the
trade-derived ladder. Every season CI excludes zero on the same side.

Watch coefficient `signed_ofi` (last-window fit, day-resampled): 4h −0.287
[−0.316, −0.274]; 1h −0.229 [−0.236, −0.194]; 15m −0.040 [−0.066, −0.017].
Negative in all three windows (Alb25's counter-trade sign), but the linear
offset still loses on RPS.

#### 1.4.1 Sign verification: the crossed-state rate

The only independent check on d is a property the mapping implies but the mapping did not
manufacture. Replay prints per ticker into the trade-implied book. If d is right, ask prints
land above bid prints most of the time and a crossed state (`ask < bid`) is occasional
staleness. If d is inverted, the anchor is crossed nearly always and the rate approaches 100%.
The inverted-d replay is run as a paired control on the same prints, so the two rates are
reported together.

A synthetic fixture cannot settle this — the fixture is built from the same assumption it
would be testing. The number must come from the real corpus, and it must be reported before
anything is fitted.

One caveat on how the number is presented. Flipping d swaps which side each print
writes, so the inverted rate is the exact complement of the as-specified rate except
where `ask == bid`. The inverted column is a reading aid, **not corroboration**; the
evidence is the *level* of the as-specified rate against the 0.5 coin flip, which is a
property of the prices and not of any label field.

| Item | Status |
|---|---|
| Diagnostic | `wxmm.fairvalue.crossed.crossed_state_rate` |
| Runner | `python -m analysis.crossed_rate --parquet data/trades/KXHIGHNY` |
| Gate | `wxmm.fairvalue.v0_min.run_c1_m1_v0_min` refuses to fit on `sign_inverted` |

#### 1.4.2 Result: crossed rate 5.82% `[V-LOCAL]`

Run 2026-09-15 on the full pulled corpus (3,405,461 prints, 6,974 tickers, 1,374
climate days 2022-12-11 → 2026-09-15, `analysis/out/crossed_rate.json`):

| Quantity | As specified (`yes` → ask) | Inverted d (mirror) |
|---|---|---|
| Crossed rate | **0.0582** (195,892 / 3,368,606) | 0.8277 |
| Median implied spread | **+2.0c** | −2.0c |
| Ties (`ask == bid`) | 384,667 | 384,667 |
| Tickers majority-crossed | **230 / 6,974** | 6,074 / 6,974 |

An earlier partial pull (2.83M prints, through the 2026-07-16 historical cutoff only)
gave 0.0625 on the same corpus definition, so the number is not sensitive to the
sample boundary.

**The sign is confirmed.** Trade-implied ask sits above trade-implied bid 94.2% of the
time and the median trade-implied spread is positive, which is what a correctly oriented
anchor looks like. An inverted sign would have driven this toward 100% and the median
spread negative. Crossing at 5.8% is the staleness the anchor's docstring predicts — the
two sides are drawn from different instants — not a direction error. Only 3.3% of tickers
are majority-crossed.

This was run before the first fit, and the fit path refuses to proceed on a
`sign_inverted` verdict.

---

## 2. NBM gridded archive (AWS)

### 2.1 Location & layout

- Bucket: `s3://noaa-nbm-grib2-pds` (NODD program), HTTP root
  `https://noaa-nbm-grib2-pds.s3.amazonaws.com/`, region us-east-1, no credentials. `[V-LOCAL]`
- Path: `blend.YYYYMMDD/CC/{core|qmd|text}/`. File names
  `blend.tCCz.core.fXXX.RR.grib2`, `blend.tCCz.qmd.fXXX.RR.grib2`; CONUS region code `co`.
  Text bulletins `blend_nb{h,s,e,x,p}tx.tCCz`. `[V-LOCAL]`, layout doc: MDL NBM Data Download
  page (`vlab.noaa.gov/web/mdl/nbm-download`) `[V-PRIMARY]`.
- **Every grib2 file has a `.idx` sidecar** — a wgrib2-style plain-text inventory
  (`msg:byte_offset:d=YYYYMMDDCC:VAR:level:fcst:info`). Inventory questions and byte-range
  subsetting need no grib download. `[V-LOCAL]`
- Archive floor: `blend.20200517/` empty, `blend.20200518/` populated, stamped V3.2 →
  **floor 2020-05-18**. `[REPORTED]` (single run; MDL prose says "about May 2020") `(verify at first backfill)`.
- Pinned reference artifacts (2026-01-01 00Z, CONUS, f001) `[V-LOCAL]`:
  - `core`: 159,021,586 B, SHA-256 `8B39C4FB066DA456E245F68CF054015A4FD5DFC57C59EF152F078DC1A7C238B7`
  - `qmd`: 283,013,642 B, SHA-256 `25ACFE5B58979099459BBCDCB853DF966C51515E42AEDF120A1196129DA8D3C9`

### 2.2 Live vs archive (DECIDED)

- NOMADS holds the live feed with ~1–2 days retention:
  `https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod/blend.YYYYMMDD/CC/` `[V-PRIMARY]` (layout page).
- Same-day `qmd` was **absent from the AWS mirror intraday** on 2026-08-12 while present for
  completed days — an AWS sync lag, and the root cause of one instrument's false
  "qmd discontinued" claim. `[CORR]` (artifact reconstructed from both runs)
  `(verify lag magnitude — O3)`.
- **Decision: prospective pipeline pulls from NOMADS in near-real-time; AWS is backfill only.**

### 2.3 Cycles & latency

- Hourly cycles; **text-bulletin** publication ~30–50 min after nominal cycle time; primary
  full-content cycles 01Z/07Z/13Z/19Z. `[REPORTED]` (VLab text-products page, quoted round 1)
  — applies to **text bulletins only**, not qmd grib percentile products.
- **qmd grib percentile products (CONUS window max/min):** measured prospective
  first-availability on NOMADS across 3 cycles (404→200 polling, Session 6b-fix) with AWS
  `Last-Modified` corroboration. `[V-LOCAL]` — 2026-08-21/22:
  - median **435 min**, p90 **441 min**, max **453 min** after nominal cycle time
  - Method: `analysis/nbm_availability_watch.py` on NOMADS; Panel A AWS Last-Modified ~438 min
  - **Vintage rule (Session 7e):** T−24h snapshot selects latest 00Z/12Z cycle with
    `publication_utc(nominal, 441) < snapshot` and idx-confirmed 12Z–06Z(+1) max window;
    expected cycle **D−1 12Z / f042** (~24h forecast lead), verified empirically per day
  - Prior 300-day backfill at 60 min / D 00Z f030 is **void** (future information at snapshot)

---

## 3. NBM version eras & temperature-distribution content (CONUS)

### 3.1 Era table

| Era | In effect (archive-empirical) | Distributional temperature content (CONUS grib) |
|---|---|---|
| v3.2 | 2020-05-18 → 2020-09-28 | None beyond hourly deterministic TMP + ens std dev. **Unusable; pre-dates market volume.** |
| **v4.0 → v4.3** | **2020-09-29 → 2026-05-03** | **`qmd`: 99-level percentile ladder (1%…99%) of the 18-hour min/max temperature window**, 12-hourly at f018+12k (f018…f270), plus windowed value line, windowed StdDev line, and 8 fixed threshold exceedances. `core`: hourly deterministic 2-m TMP + ens std dev. |
| v5.0 | 2026-05-04/05 → present | `qmd` adds **instantaneous hourly 2-m TMP percentiles, 21 levels (0%…100% by 5)**, onset 2026-05-04; the 18-h window products **drop from 99 to 21 levels** on 2026-05-05. APTMP/DPT/RH percentile ladders also added. |

Sources: percentile-onset bisection lands exactly on 2020-09-29 `[REPORTED]`, which equals the
announced v4.0 implementation date `[REPORTED — VLab NBM Versions prose]`; the two are
independent, so the v4.0 boundary is treated as `[CORR]`. v5.0 instantaneous-temperature
percentiles match PNS 25-45 item (3) `[V-PRIMARY — PDF fetched in-session,
weather.gov/media/notification/pdf_2025/pns25-45_soliciting_comments_NBM_v5.0.pdf, 2025-07-15]`.
Window-product structure matches byte-identical idx lines from both runs `[CORR]`.

Version implementation dates (VLab NBM Versions prose + SCN 25-34; cross-checked against
archived bulletin header stamps) `[REPORTED, cross-checked]`:
v4.0 2020-09-29 · v4.1 2023-01-17 · v4.2 2024-05-15 · v4.3 2025-04-15 · v5.0 announced
2026-05-05 (data boundary 05-04, see 3.3).

### 3.2 The 18-hour window products (retrospective benchmark backbone)

- Encoded as `TMP` with a window statistic — **not** `TMAX`/`TMIN`. Verbatim idx line formats
  (parser contract) `[CORR — byte-identical across runs]`:

```
NNN:BYTEOFF:d=YYYYMMDDCC:TMP:2 m above ground:A-B hour max fcst:P% level    (P = 1..99 pre-v5.0; 0,5,..,100 post)
NNN:BYTEOFF:d=YYYYMMDDCC:TMP:2 m above ground:A-B hour max fcst:            (windowed point value)
NNN:BYTEOFF:d=YYYYMMDDCC:TMP:2 m above ground:A-B hour StdDev fcst:
NNN:BYTEOFF:d=YYYYMMDDCC:TMP:2 m above ground:A-B hour min fcst:prob <233:prob fcst 255/255
```

- Forecast hours: exactly f018, f030, f042, … f270 (multiples of 6 that are ≡ 6 mod 12).
  Max/min alternate by cycle: 00Z f018 = 0–18h **min**, 12Z f018 = 0–18h **max**, then
  alternating every 12 h. `[CORR]`
- **Window semantics** (v5.0 station card, applies to the TXN element and matches the grib
  windows): min window 00Z–18Z reported 12Z; **max window 12Z (current day) – 06Z (next day),
  reported 00Z following day**. `[V-PRIMARY — vlab.noaa.gov/web/mdl/nbm-textcard-v5.0, fetched in-session]`
- The max window 12Z–06Z ≈ local 7/8 AM – 1/2 AM next day. This is **close to, but not equal
  to, the CLI climate day** — it excludes the local early-morning hours. It is materially better
  aligned than the NDFD "MaxT" element (7 AM–7 PM LST daytime window, NWSI 10-201 `[REPORTED]`),
  which is **not used** in this project. **Decision: quantify the mismatch empirically** — the
  rate at which KNYC's calendar-day max falls outside 12Z–06Z, computed from the CLI archive —
  and carry it as a measured number, not an assumption.

### 3.3 v5.0 transition (segment boundary — REQUIRED in every analysis spanning it)

- **2026-05-04:** hourly instantaneous TMP percentiles appear (21-level). **2026-05-05:**
  window products drop 99→21 levels. 2026-05-04 is a mixed day (some hours 99-level, some 21).
  Treat **2026-05-04/05 as a transition pair**, and cut backtest segments at 2026-05-04.
  `[REPORTED]` (final Perplexity run; direction consistent with PNS 25-45) `(verify at first parse — O1)`.
- The boundary is simultaneously a **model upgrade** (incl. ECMWF-AI/ECAIFS input added as a
  temperature predictor, PNS item 18 `[V-PRIMARY]`) and a **benchmark-resolution degradation**
  (99→21 levels). Any market-vs-NBM series spanning it changes instrument mid-series.
- **2026-07-28:** v5.0 temperature patch "to improve temperatures during seasonal transition
  periods" `[REPORTED — VLab prose]` — sits *inside* our prospective logging window; second
  segment marker.

### 3.4 Hourly fields (short-horizon leg)

- `core`: hourly deterministic 2-m TMP + ens std dev, present in all eras (verified at f001,
  f013, f025). `[CORR]` Hourly extent by era: 36 h pre-v5.0, 48 h v5.0 (PNS item 17
  `[V-PRIMARY]` for the extension; exact per-era grids `(verify — O6)`).
- v5.0 only: hourly 21-level TMP percentile ladder at every hourly step (verified f013, f025).
  `[REPORTED]` `(verify — O2: reported 42 percentile lines/file vs 21 expected levels; likely
  duplicate encodings; resolve at first parse)`.
- Building a **calendar-day max from hourly marginals requires a temporal-dependence
  assumption** (independence vs comonotone bounds the answer; truth between). Not needed for the
  retrospective leg — the window product is already a max distribution — but unavoidable if the
  hourly path is used to cover the window/climate-day gap or sub-daily horizons.

---

## 4. NBM station text bulletins (KNYC-resolved, no grib tooling)

- Five products `[V-PRIMARY — v5.0 station card]`:

| Product | Type | Step | Hours (00/12Z cycles) |
|---|---|---|---|
| NBH | Hourly | 1 h | 1–25 |
| NBS | Short | 3 h | 6–72 |
| NBE | Extended | 12 h | 24–192 |
| NBX | Super-extended | 12 h | 204–264 |
| NBP | Probabilistic | 12 h | 24–228 |

- **KNYC (Central Park) has rows in all five products**, alongside KLGA/KJFK/KEWR — no airport
  proxy needed. `[CORR]` — verbatim headers (`KNYC    NBM V4.3 NBH GUIDANCE    1/01/2026  0000 UTC`)
  from archived in-bucket bulletins, both runs.
- Relevant elements `[V-PRIMARY — station card]`: NBH/NBS/NBE: `TMP`/`TSD` (hourly/3-hourly temp
  ± SD), `TXN`/`XND` (18-h max/min ± SD, windows as §3.2). **NBP: `TXNMN`, `TXNSD`, and
  quantiles `TXNP1/P2/P5/P7/P9` = 10th/25th/50th/75th/90th percentile of the 18-h min/max
  (min listed 12Z, max 00Z); temperature rows CONUS+AK only.** NBP full content only at
  01Z/07Z/13Z/19Z cycles; starts at FHR 24, so the **nearest-day max is not covered** — short
  horizons come from grib (§3) or hourly fields.
- Bulletins are mirrored **inside the AWS bucket** (`blend.YYYYMMDD/CC/text/`), verified for
  2026 dates `[CORR]`; claimed present back to 2020-06-01 `[REPORTED]` `(verify — O5)`.
  How far back NBP carries `TXNP*` rows: `(verify — O4)`; v5.0 card states bulletins
  "more or less unchanged" from v4.3.
- IEM also archives the bulletin PILs point-in-time (platform floor 1996) — fallback path,
  exact PILs `(verify)`.

---

## 5. Benchmark architecture (decisions that follow from the facts)

1. **Retrospective leg (2020-09-29 → 2026-05-03, i.e. the entire era Kalshi KNYC markets have
   traded):** primary benchmark = the 99-level 18-h **max-window percentile distribution** at the
   Central Park grid point (or NBP `TXNP*` quantiles at station level for medium horizons).
   No Gaussian mean+σ approximation; no max-from-hourly reconstruction; no hourly-dependence
   assumption needed.
2. **Prospective leg (v5.0 era):** window product at 21 levels + hourly 21-level ladders;
   NOMADS-live ingestion. Carries both 2026-05-04/05 and 2026-07-28 regime markers, and (as of
   drafting) spans only the calm summer regime — verdicts are season-stamped.
3. **Window-vs-climate-day mismatch** is the one standing benchmark caveat; it is to be
   *measured* from the CLI archive (§3.2 decision), then carried as a number next to every
   comparison.
4. NDFD MaxT (7 AM–7 PM) is **not** an input.
5. KNYC grid-point coordinates: Central Park ≈ 40.779 N, 73.969 W; exact station coordinates
   and nearest-gridpoint policy `(verify — O8)`. CONUS grid ≈ 2345×1597 (Herbie doc,
   secondary) `(verify)`.
6. **The retrospective leg covers 100% of market history.** Kalshi NYC-high market history
   begins **2021-08-05** — 10 months *after* the v4.0 boundary (2020-09-29), so the 99-level
   percentile ladder of §3.1 is available for every market-day we can ever backfill. There is
   no pre-v4.0 market segment to carve out, and no era boundary inside the retrospective leg
   other than v4.1/v4.2/v4.3 (which do not change the 99-level window instrument).
   `[V-LOCAL]` — `python -m ingestion.kalshi_history --dry-run`, 2026-08-14: 9,364 markets,
   `date_span_open_close: 2021-08-05 to 2026-08-13`.
7. **Early-market regime break (2021).** Bracket density was ~1.3 markets/day in 2021 vs ~6/day
   from 2022 on (by close year: 2021: 197, 2022: 1250, 2023: 2181, 2024: 2196, 2025: 2190,
   2026-partial: 1350). The 2021 market was structurally different — materially fewer brackets
   per day — so 2021 is reported as a **separate regime** in any liquidity or bracket-count
   readout and never pooled with 2022+. `analysis/spread_census.py` enforces this with an `era`
   column. `[V-LOCAL]` — same dry-run.
8. Kalshi's own contract text names the settlement source: `rules_primary` cites the "National
   Weather Service's Climatological Report (Daily)" verbatim, corroborating §1.2's choice of
   CLINYC from the venue side. Details in `venue-facts.md` §1.3.

---

## 6. Residual opens

| # | Item | Blocking? | Resolution path |
|---|---|---|---|
| O1 | Perplexity gloss claimed "99 TMP lines in f024" once, contradicting both hour maps | No | First parser run enumerates hours; discard or explain |
| O2 | 42 vs 21 instantaneous percentile lines per v5.0 hourly file (duplicate encodings?) | No | Inspect one idx fully at first parse |
| O3 | AWS same-day `qmd` sync-lag magnitude/pattern | No (NOMADS decision already made) | Log AWS-vs-NOMADS arrival deltas for a week |
| O4 | Era depth of NBP `TXNP*` rows (v4.0? v4.2?) | No | grep archived `blend_nbptx` at 2021/2023 dates |
| O5 | In-bucket `text/` presence back to 2020-06-01 | No | Spot-probe 3 dates during backfill |
| O6 | Hourly `core` grid extent by era (36 h vs 48 h; 3-hourly beyond) | No | idx enumeration during backfill |
| O7 | One-click confirmation of `[REPORTED]` URLs (NWSI 10-1004, 10-201, lot-FAQ, ndfd_metadata, NBM Versions) | Before citing externally | 10 minutes of clicks; fix links in place |
| O8 | Exact KNYC station coords + gridpoint-selection policy | Before first grid extraction | MDL station table / bulletin metadata |
| O9 | §1.3 label selection needs a per-era settlement snapshot hour (10 AM before 2024-09-04, 7/8 AM after, unspecified before 2021-12-28). One fixed hour silently mis-selects the issuance, which is the exact silent-label-error class §1.3 exists to prevent | Before building labels for pre-2024-09-04 market days | Take era bounds from `venue-facts.md` §1.10; resolve the 2021 era from Rulebook Rule 100.19 (venue lane, V1) |
| O10 | 4 market climate days have no usable CLINYC label: 2025-06-02 / 2025-06-03 (no issuance archived) and 2025-06-18 / 2025-11-13 (same-day intermediate only). Kalshi settled those markets on something | Before labelling those 4 days | Re-query IEM per-issuance (`p.php`) for the exact dates; if genuinely absent, exclude the days and record the exclusion |

**Decisions pending in other lanes (not this file's to make):** Polymarket logger inclusion
(root chat, recorded decision); knowledge-file naming set (root chat); Chicago settlement
station (venue lane; vendor lead says Midway `(verify)`); Kalshi collateral netting across
brackets (venue lane, rulebook-PDF method); Kalshi candlestick/order-book endpoint specs
(venue lane → `venue-facts.md`).

## 7. Known instrument failure modes (recorded so they aren't repeated)

1. **Sampling grids are hypotheses.** The f012/f024/f036/f048 grid stepped exactly over the
   f018+12k window products; two AI tools independently "confirmed" the same false absence.
2. **Grep for the encoding, not the concept.** Window max/min temperature is `TMP` + window
   statistic; `grep TMAX|TMIN` returns a true 0 that means nothing.
3. **Same-day archive listings lie.** Intraday AWS sync lag produced a false
   "product discontinued." Never generalize from the current UTC day's directory.
4. **Citation links from AI research runs are unreliable even when the prose is right.**
   Round-1 output attached unrelated URLs to correct claims. Links enter this file only after
   a human or Claude has resolved them (`O7`).
5. **Two AIs agreeing is corroboration, not ground truth** — especially when both were handed
   the same flawed instrument. Local reproduction with pinned artifacts (hashes) is the standard.
6. **A 200-shaped pipeline with an empty output is a failure mode.** The CLINYC backfill
   initially used `limit=10000` on IEM `retrieve.py`, which returns HTTP 422 (pydantic
   validation: max 9999) for every month. The script treated any HTTP response as success,
   wrote an empty `clinyc.csv`, and marked each month complete — a silent total miss. Fixed
   2026-08-14 by pinning `IEM_MAX_LIMIT = 9999`, checking status codes, and asserting
   non-empty outputs when a month is marked complete. `[V-LOCAL]`
