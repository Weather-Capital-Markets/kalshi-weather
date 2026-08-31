# plan.md — Kalshi KXHIGHNY session 2 execution plan

**Status:** ACTIVE — source of truth for run order, gates, and K1 pre-registration.
**Owner:** root chat ratifies; this file records what landed in code.

## 1. Scope

Session 2 on the laptop only: bulk historical fetch, CLINYC labels, emission validation,
era-boundary analysis, and spread census. The VPS logger (`heartbeat.sqlite`) is out of scope
here but load-bearing for K4 accrual and the forward quote-emission cross-check named in K1 v3.

## 2. Run order and gates

```text
bulk fetch → CLINYC labels → validate_candles → venue_eras → [ratification] → spread_census
```

Steps 4 and 5 must not overlap (`backfill.sqlite` has no WAL). The census is gated on **K1 v3
ratified in the root chat** (with date filled in), not on the pre-registered exact-equality
`VOLUME_RECONCILE` FAIL from v2. v2 is void by its own validity clause.

## 3. K1 pre-registration

### v3 (current — supersedes v2, voided per its own validity clause)

> **K1 pre-registration v3 (supersedes v2, voided per its own validity clause — date: ____)**
>
> Primary statistic and thresholds unchanged from v2: median quoted spread at T−24h,
> carry-forward book states, two-sided only (bid ≥ 1¢, ask ≤ 99¢), mid 10–90¢, in trading
> window, 2022-01-01 onward (2021 separate). ≥ 4¢ kill; 2–4¢ taker dead, maker live; < 2¢ → K2.
>
> Robustness columns: (a) 15-minute staleness rule; (b) exclusion of the 47 non-reconciling
> markets (34 climate days). Kill-direction disagreement in either → written discussion, no
> automatic verdict.
>
> Validity, recorded: VOLUME_RECONCILE passed at 9,317/9,364 exact, residual 0.0052% of volume,
> with 7 over-reconciliations inconsistent with capture loss → capture accepted, discrepancies
> attributed to venue bookkeeping (venue-lane item V3). Quote-only emission completeness is
> unverifiable for history; a forward cross-check (VPS logger books vs same-period candles,
> ≥ 3 days × ≥ 3 active markets) is a standing obligation — failure caveats the historical
> census and voids prospective (2b) use, but does not retroactively void a K1 verdict.
>
> Integrity note: v3 drafted after validation outputs were seen, before any spread/census output
> existed.

### v2 (void)

v2 required exact volume reconciliation and a live-dense control. Both failed honestly:
VOLUME_RECONCILE at 9,317/9,364; live-tier gap share 22.8% (not dense). v2 is void by its own
validity machinery; the census stayed frozen until v3 was registered here.

## 4. State (2026-08-14)

| Item | Status |
|---|---|
| Bulk fetch | **Done** — 9,364/9,364 markets, 5.25 M candles, 87 MB on disk (0.9% of 9.94 GB projection) |
| CLINYC backfill | **Done** — 4,913 issuances, 2,413 full-day labels, **1,825/1,829** market climate days covered |
| Four missing label days | 2025-06-02, 2025-06-03 (no CLINYC in IEM archive); 2025-06-18, 2025-11-13 (same-day intermediate only) — see `data-sources.md` O10 |
| `validate_candles.py` | **Run** — EMPTYING_EMITTED PASS; VOLUME_RECONCILE FAIL at pre-registered exact equality; accepted per v3 validity |
| `venue_eras.py` | **Run** — last trading time change 2026-03-17→18; settlement eras in `venue-facts.md` §1.10 |
| Census code | **Ready** — carry-forward primary, `_strict15` + `_exclnoreconcile` robustness columns |
| Polymarket logger | **Done** — Gamma `nyc-daily-weather` ladder + CLOB books; PR #10 on `main` |
| VPS dual logger | **Runbook** — `scripts/vps-setup.sh`; `scripts/verify-setup.sh` for local smoke |
| Census execution | **Frozen** — awaiting v3 date in root chat + VPS `--status` |
| PR #2 | Bulk backfill branch ready to merge after this housekeeping |
| Open (load-bearing) | VPS `--status` (K4 + forward quote validation); root-chat ratification date; Polymarket; Gate 0 |

## 5. Session 2.5 — instrument calibration + forward-validation tooling

**Status:** IN PROGRESS — laptop only. No census execution, no NBM ingestion, no models.

| Item | Script | Notes |
|---|---|---|
| A1 ASOS pull | `ingestion/asos_obs.py` | IEM `NYC`/`NY_ASOS`/`tmpf`, monthly, `category=asos_obs`, sample from 2025-05-01 |
| A2 Clock B check | `analysis/clockb_check.py` | ~15 sampled labeled days; LST vs LDT hypothesis table; measurement only |
| B Window mismatch | `analysis/window_mismatch.py` | CLI time-of-high vs NBM 12Z–06Z window; refuses headline when convention=unknown |
| C Forward emission | `analysis/validate_emission_forward.py` | Logger orderbook JSONL vs live candles; read-only, never `heartbeat.sqlite` |
| D Label-less days | `tests/test_spread_census.py` | Four missing-label climate days degrade gracefully |
| Config | `climate.cli_time_convention` | `lst` \| `ldt` \| `unknown` (default) → current ±1h `regime_uncertain` behavior |

```text
asos_obs → clockb_check → [write Clock B conclusion to data-sources.md]
window_mismatch (K2 prep)
validate_emission_forward (run when VPS alive; rsync or on-box)
```

**Code landed 2026-08-14** — 71 tests passing; census still not executed.

## 6. Session 3 — depth census (K1 2b) + forward emission + Polymarket logger

**Status:** IN PROGRESS — VPS logger data; no NBM, no models.

| Item | Script | Notes |
|---|---|---|
| A Depth census | `analysis/depth_census.py` | Logger orderbook JSONL only; horizons T-24h…T-1h; 10–90¢ band |
| B Forward emission | `analysis/validate_emission_forward.py` | Logger vs live candles; silent-gap count; shortfall reporting |
| C Polymarket logger | `ingestion/polymarket_logger.py` | Gamma `series_slug=nyc-daily-weather`; full daily ladder; CLOB books; `pm_meta` strike/direction |

```text
depth_census → validate_emission_forward (when logger has ≥3d raw)
polymarket_logger --probe → --once on VPS alongside Kalshi logger
```

Polymarket logging is **measurement only**. Deferred in code comments: (a) S2 bracket sum
law on Polymarket; (b) cross-venue bracket-to-bracket comparison (KLGA vs KNYC basis).

## 7. Session 4 — KLGA–KNYC station basis measurement

**Status:** IN PROGRESS — analysis only; no models, no trading logic, no NBM.

| Item | Script | Notes |
|---|---|---|
| A Dual-station ASOS pull | `ingestion/asos_obs.py` | IEM `NYC` + `LGA` / `NY_ASOS`; resumable per station×month; `2021-01-01`→present |
| B Station basis | `analysis/station_basis.py` | LST daily max; Polymarket even-edged bracket disagreement rate; distributions only |

```text
asos_obs → station_basis
```

Load-bearing for venue-facts §3.2 / gate decision on cross-venue comparison. Measures
**IEM-ASOS station spread only** — WU provider gap documented as lower bound in NOTES.

## 8. Session 5 — turnover census (Gate 0 capacity)

**Status:** IN PROGRESS — laptop only; frozen historical candlesticks, no network.

| Item | Script | Notes |
|---|---|---|
| A Turnover census | `analysis/turnover_census.py` | Contracts + premium traded per market/climate day; 10–90¢ / tails / all bands; distributions only |

```text
spread_census loaders → turnover_census
```

Measures **premium and contracts traded** on Kalshi KXHIGHNY — an upper bound on maker
capture (fill share is K4). No Gate 0 arithmetic in code. Headlines: market-day medians for
2022+ 10–90¢ band, median brackets-with-volume per climate day, zero-volume market-day
fraction. Outputs: `turnover_census.csv` + three PNGs (volume by season, vs time-to-close,
by price region). Streams one market at a time to avoid loading the full corpus into RAM.

## 9. Session 6a — K2 prerequisites (window mismatch K2 + NBM archive)

**Status:** IN PROGRESS — laptop only; byte-range NBM qmd archive + extended window mismatch.

| Item | Script | Notes |
|---|---|---|
| A Window mismatch K2 | `analysis/window_mismatch.py` | `window_mismatch_k2.csv` + PNG: CLI lst/ldt + ASOS KNYC outside 12Z–06Z; conditional `delta_f`; bracket disagreement (`whole_f`); both clock hypotheses, no pick |
| B NBM idx parser | `ingestion/nbm_idx.py` | `.idx` line parse, byte ranges, empirical vintage (441/453 min qmd latency), era/version tags |
| C NBM archive | `ingestion/nbm_archive.py` | AWS `noaa-nbm-grib2-pds`; range-only via `.idx`; `--vintage-calibrate` / `--probe` / `--dry-run` / resumable backfill; nearest gridpoint for O8 |

```text
window_mismatch (K2 CSV) → nbm_archive --vintage-calibrate → nbm_archive --probe
nbm_archive --dry-run → nbm_archive (resumable backfill to data/nbm/decoded_v441)
```

NBM retrospective scope: **300 stratified climate days** (season × v4 early/late sub-era,
seed **43**, start **2022-12-11**) with **9 percentile levels** (P10–P90 by 10) of the
climate max-window ladder — not the full ~1,700-day × 99-level corpus. Prior backfill at
`data/nbm/decoded/` (60 min / D 00Z f030) is **void**; corrected writes go to
`data/nbm/decoded_v441/`. `nbm_archive --dry-run` calibrates bytes from empirical vintage
`.idx` sidecars only; bulk backfill waits on approval after probe + dry-run.

Eligible span `2022-12-11` → `2026-05-03` (hard cut 2026-05-04). cfgrib +
pyarrow in `requirements-analysis.txt`. Raw `nbm_qmd` JSONL per percentile message.

## 10. Session 6b — K2 blocking prerequisites (bracket structure + NBM latency)

**Status:** IN PROGRESS — measurement only; Session 6c comparison gated on these outputs.

| Item | Script | Notes |
|---|---|---|
| A Bracket enumeration | `analysis/bracket_enumeration.py` | Frozen `markets_history` only; derive width, alignment, contiguity, tails; `bracket_structure.csv` |
| B NBM latency check | `analysis/nbm_latency_check.py` | HEAD on AWS qmd `.idx`; mirror lag vs 441 min assumption (config default); **hard stop if p90 > assumed** |
| B-fix Availability watch | `analysis/nbm_availability_watch.py` | Prospective first-HTTP-200 poll AWS + NOMADS; resolves Last-Modified vs real lag (**blocking K2**) |

```text
bracket_enumeration → nbm_latency_check → nbm_availability_watch → (6c gated)
```

Do **not** build forecast-vs-market comparison until A and B pass. B-fix settled qmd
latency at p90 **441 min** (max **453 min**); Session **7e** re-runs `nbm_archive` with
empirical D−1 12Z / f042 vintage + idx-confirmed max window. **Do not delete** the void
`data/nbm/decoded/` backfill until `decoded_v441/` completes.

## 11. Session 7e — NBM vintage correction + backfill re-run

**Status:** DONE — 300/300 climate days backfilled to `data/nbm/decoded_v441/` (seed 43, start 2022-12-11). All days use D−1 12Z / f042 at 441 min latency; 0 skips.

| Item | Module | Notes |
|---|---|---|
| A Empirical vintage | `ingestion/nbm_idx.py` | Candidate pool at max latency; V1 assert at p90; idx-confirmed climate max window |
| B Ladder repair | `ingestion/nbm_ladder.py` | Dedupe `(climate_date, percentile_level)`; isotonic PAV for non-monotone quantiles |
| C Archive wiring | `ingestion/nbm_archive.py` | `--vintage-calibrate`; writes `decoded_v441/` + `backfill_v441.sqlite` |
| D Config | `ingestion/config.yaml` | `publication_latency_min: 441`, `start_date: 2022-12-11`, `sample_seed: 43` |

```text
nbm_archive --vintage-calibrate → nbm_archive --probe → nbm_archive --dry-run → nbm_archive (done 2026-08-24)
```

## 12. Session 6c — NBM forecast vs market at T-24h

**Status:** IN PROGRESS — measurement only on 300-day NBM sample.

| Item | Script | Notes |
|---|---|---|
| A Forecast vs market | `analysis/forecast_vs_market.py` | NBM bracket probs vs Kalshi carry-forward mid at T-24h; Brier + edge distributions |
| B Bracket structure | `analysis/bracket_enumeration.py` | Prerequisite metadata (`bracket_structure.csv`) |

```text
bracket_enumeration → forecast_vs_market
```

Uses `decoded_v441/` only (void `decoded/` excluded). Primary band 10–90¢ matches K1.
Measurement only — no pass/fail verdict on forecast skill vs market.

## 13. Session 8 — S2 relative-value census

**Status:** FAILED — S2 v1 dated 2026-08-30 (V4 amended as a threshold branch from coverage facts only). Coverage 2026-08-29. Violations + penny-exclusion measured after the dated text. Verdict 2026-08-30: FAIL (after-fee slack and ROC; frequency MARGINAL). Strategy DEAD.

| Item | Module | Notes |
|---|---|---|
| A Coverage | `analysis/relative_value_census.py --phase coverage` | Complete-book rates T−36/24/12/6h; no sums |
| B Pre-registration | this file, S2 v1 | Written 2026-08-30 after coverage, before magnitudes |
| C Violations | `--phase violations` | Executable sums, persistence ≥2 candles, after-fee slack, ROC, penny-exclusion |
| D Verdict | this file, S2 verdict | 2026-08-30 FAIL; two independent FAIL clauses |

### S2 pre-registration v1 (date: 2026-08-30)

> **S2 PRE-REGISTRATION v1 — 2026-08-30**
> Ratified by: Eugenio
> Written after coverage rates were seen, BEFORE any violation magnitude exists.
>
> QUESTION: Do Kalshi's mutually-exclusive, exhaustive daily temperature
> brackets exhibit executable coherence violations (sum of asks < 1, or sum of
> bids > 1) frequently and largely enough to constitute a business? S2 requires
> no forecast; K2's failure does not bear on it.
>
> UNIVERSE: all KXHIGHNY/HIGHNY climate days from 2022-12-11 (stable 6-bracket
> regime), 1,340 six-bracket days. Carry-forward quotes, two-sided defined as
> bid >= 1c and ask <= 99c, snapshot inside the trading window.
>
> COVERAGE (measured, recorded before thresholds):
>   T-36h 34.5% complete books (457/1,325)
>   T-24h 22.4% (299/1,337)
>   T-12h  1.0% (14/1,340)
>   T- 6h  0.1% (2/1,340)
> Mean two-sided legs 4.77 -> 4.23 -> 2.93 -> 0.59. At T-6h, 918/1,340 days have
> ZERO two-sided legs.
>
> PRIMARY HORIZON: T-24h. T-12h and T-6h are EXCLUDED from threshold evaluation
> (n=14 and n=2); they are reported descriptively only. T-36h is reported as a
> secondary horizon.
>
> PRIMARY STATISTIC: violation-days per year, defined as
>   (complete-book rate) x (fraction of complete books with a violation
>    surviving >= 2 consecutive candles) x 365
> Persistence is required: a single-candle crossing is a microstructure artifact,
> not a tradeable state.
>
> SECONDARY, REQUIRED FOR ANY PASS: median slack among persistent violations,
> after the published quadratic taker fee per leg, and the implied return on
> collateral under the buy-the-set formula (capital = sum_ask).
>
> THRESHOLDS:
>   PASS      : >= 30 violation-days/yr AND median after-fee slack >= 2c AND
>               per-trade ROC >= 0.5% AND V4 tradeability.
>               Rationale: 30 days/yr at ~2c on a basket is the minimum that
>               could contribute meaningfully against Gate 0's $24,000, given
>               measured turnover of ~$2,400 premium per climate day.
>   MARGINAL  : violation-days 10-30/yr, or slack 1-2c. Report, do not act;
>               the pre-registered response is a depth measurement on logger
>               books, NOT reinterpretation.
>   FAIL      : < 10 violation-days/yr OR median after-fee slack < 1c OR
>               ROC < 0.5% OR V4 not tradeable.
>
> VALIDITY CONDITIONS:
>   V1. Violations counted only on complete books (all six legs two-sided).
>       A five-leg book is not a coherent basket.
>   V2. Persistence >= 2 consecutive candles required, as above.
>   V3. Slack must be reported both raw and after the published quadratic taker
>       fee ceil_6dp(0.07*P*(1-P)) applied per leg. The live fee page was not
>       re-verified this session (Cloudflare-blocked); if the published schedule
>       is later found stale, this pre-registration is void.
>   V4. Report the primary statistic separately on (a) all complete books and
>       (b) the all-legs-in-10-90c subset (n=12 at T-24h, n=14 at T-36h). A
>       PASS requires the violation to be present in (b), or to be present in
>       (a) with slack that survives excluding any leg quoted at <=1c or >=99c.
>       A violation whose slack derives from a penny leg is recorded as NOT
>       tradeable regardless of magnitude. Given n~12, subset (b) cannot
>       support a statistical claim -- it is a necessary check, not a
>       sufficient one.
>   V5. Depth is NOT measured. Candles carry no resting size. Any PASS is
>       therefore conditional on a subsequent logger-based depth check, and no
>       capital is committed before it.
>
> RECORDED IN ADVANCE: the coverage pattern already constrains the outcome. At
> 22.4% complete books at T-24h, even a 20% violation rate among them yields
> ~16 days/yr, which lands MARGINAL. A PASS requires either a high violation
> rate or the T-36h horizon carrying it. Note also that the horizons with the
> best return-on-capital economics (T-12h, T-6h) are exactly where complete
> books do not exist -- the strategy's best regime is structurally empty. A
> further constraint is now known: 71% of T-24h complete books contain a <=1c
> leg, and only 12/299 have all six legs in the tradeable band. The population
> of genuinely executable baskets is therefore roughly 12-14 per horizon
> across three-plus years, i.e. ~4/yr. Unless violations concentrate
> overwhelmingly in that small subset, the violation-days-per-year statistic
> will land FAIL on tradeability even if raw slack looks large.
>
> INTEGRITY NOTE: coverage was known when these thresholds were set; violation
> magnitudes were not. The thresholds are calibrated to Gate 0 arithmetic
> ($24,000/yr against ~$2,400 median daily premium), not to what the data is
> likely to show.

### S2 verdict (date: 2026-08-30)

> **S2 VERDICT — 2026-08-30**
> Recorded by: Eugenio
> Scored against S2 pre-registration v1, dated before violation magnitudes existed.
>
> RESULT: S2 FAILS.
>
> T-24h (PRIMARY), universe 1,340 six-bracket days from 2022-12-11:
>   Branch (a), all complete books (299/1,337 = 22.4%):
>     buy-the-set violations: 0
>     persistent sell violations: 77 -> 21.02 violation-days/yr  [MARGINAL band]
>     median after-fee slack: -2.27c  (raw +3.0c; 8/77 positive after fee) [FAIL]
>     median buy-set ROC: -12.3%                                          [FAIL]
>   Branch (b), all six legs in 10-90c: n=12. Zero violations of any kind.
>   Penny-exclusion: of 77 persistents, 62 survive dropping <=1c/>=99c legs;
>     15 derive slack solely from a penny leg and are discarded as untradeable.
>
> T-36h (secondary): 15.15 days/yr; after-fee slack -2.0c / -3.0c; (b) n=14 with
>   one persistent sell. Cannot carry a PASS.
> T-12h / T-6h: 3 and 1 persistent sells, negative after-fee slack. Structurally
>   empty as pre-registered (complete-book rates 1.0% and 0.1%).
>
> CLAUSES FIRED: frequency MARGINAL; after-fee slack FAIL; ROC FAIL. Two
> independent FAIL clauses. V5 never opened -- ask-size present on 0% of
> complete books, so no depth check was reachable and no capital follows.
>
> MECHANISM: there are no buy-the-set violations. The only raw edge is selling
> into a ~3c overround, and the published quadratic taker fee
> (ceil_6dp(0.07*P*(1-P)), ~1.75c/leg at mid) flips the median negative. The
> persistent 1.03 mid-sum is approximately the exchange fee viewed from outside
> the book -- not a mispricing.
>
> RECORDED PREDICTION AND CORRECTION: the pre-registration predicted FAIL on
> tradeability (too few executable baskets). That constraint is real -- (b) is
> empty, the executable population is ~4/yr -- but it is NOT what caused the
> failure. Frequency reached MARGINAL. The fee caused the failure. The
> prediction was right on outcome, wrong on mechanism; recorded because the
> distinction matters for what follows.
>
> WHAT THIS DOES NOT CLOSE: the measurement is of TAKER execution, which is what
> candles support. A maker pays $0.00 on weather (verified 2026-08-15) and would
> not have the +3.0c flipped. That is not a rescue of S2 as specified -- a maker
> cannot cross the whole basket at once -- but it does mean the overround is a
> spread-capture observation, not a relative-value one, and it belongs to K4.
>
> STATUS OF THE STRATEGY SET AFTER S2:
>   S1 recalibration      DEAD (K1 taker, K2 maker)
>   S2 relative value     DEAD (this verdict)
>   S3 weather-edge       DEAD (K2)
>   Pure spread capture   UNDETERMINED -- the only surviving candidate, and the
>                         sole remaining question in the project. K4's domain.

## 14. Session 9 — GEFS ensemble resolution test

**Status:** IN PROGRESS — last untested fair-value avenue after K2 (resolution
deficit) and S2 (DEAD). Pre-reg: [`knowledge/k2-gefs-prereg-v1.md`](k2-gefs-prereg-v1.md)
dated 2026-08-30, before GEFS magnitudes. Latency is unmeasured; bulk waits on
p90 approval.

| Item | Module | Notes |
|---|---|---|
| A Latency probe | `ingestion/gefs_archive.py --latency-probe` | First HTTP 200 on unpublished cycles; ≥4 cycles; pin p90; **STOP for OK** |
| B Probe / dry-run | `--probe` / `--dry-run` | One-day decode; idx-only bytes; stop if >~30 GB |
| C Backfill | `--backfill` | `data/gefs/decoded/` + `data/backfill_gefs.sqlite`; never NBM dirs |
| D Measurement | `analysis/gefs_resolution_test.py` | Murphy GEFS/NBM/market on joined K2 T−24h rows; distributions only |

```text
k2-gefs-prereg-v1.md → gefs_archive --latency-probe → [p90 OK]
→ --probe → --dry-run → [bytes OK] → backfill → gefs_resolution_test
```

Do **not** assume GEFS publication latency. Do **not** use accumulating 0–N hour
TMAX for the climate-day max (contaminates hours before 05Z). Instantaneous
3-hourly 2 m TMP only.

