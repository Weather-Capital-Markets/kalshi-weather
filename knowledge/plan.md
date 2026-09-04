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
| Open (load-bearing) | VPS `--status` (K4 + forward quote validation); root-chat ratification date; Gate 0 |

### Current stack (2026-09-04 housekeeping)

`main` through 2026-08-21 was PR #21 (availability watch). Sessions 7a–7e and 6c lived on stacked draft PRs (#22–#27). This housekeeping branch lands that work on one reviewable tip, with:

- NBM vintage **441/453 min**, D−1 12Z / f042, `decoded_v441/` (7e)
- `forecast_vs_market.py` code (6c; **0 comparison rows** until laptop `markets_history`/`candlesticks` exist)
- Test hardening (7c) and audit reports (7a/7b/7d)
- **NBM progress DB isolated:** `nbm_archive.backfill_db` = `data/backfill_v441.sqlite`; Kalshi/CLINYC/ASOS stay on `storage.backfill_db` = `data/backfill.sqlite`

Do **not** delete void `data/nbm/decoded/` until 6c has been run against `decoded_v441/` on a machine that also has the frozen Kalshi corpus.

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
latency at p90 **441 min** (max **453 min**); Session **7e** re-ran `nbm_archive` with
empirical D−1 12Z / f042 vintage + idx-confirmed max window (300/300 days). Keep the
void `data/nbm/decoded/` tree until 6c validates `decoded_v441/`.

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

**Status:** CODE LANDED — measurement **not run**. Cloud/VPS have no `markets_history`/`candlesticks`; laptop fresh clone has no `data/`. No Brier/edge/calibration numbers exist yet.

| Item | Script | Notes |
|---|---|---|
| A Forecast vs market | `analysis/forecast_vs_market.py` | NBM bracket probs vs Kalshi carry-forward mid at T-24h; Brier + edge distributions |
| B Bracket structure | `analysis/bracket_enumeration.py` | Prerequisite metadata (`bracket_structure.csv`) |

```text
bracket_enumeration → forecast_vs_market
```

Uses `decoded_v441/` only (void `decoded/` excluded). Primary band 10–90¢ matches K1.
Measurement only — no pass/fail verdict on forecast skill vs market.
Windows: `scripts/run-session6c.cmd` (checks `markets_history`, `candlesticks`, `decoded_v441`).

## 13. Sessions 7a–7d — audits (reports landed)

| Session | Artifact | Status |
|---|---|---|
| 7a correctness | `knowledge/audit-correctness.md` | Report; C1 vintage remediated by 7e; C2 empty-CLINYC remediated by 7c; C3 empty-lag hard-stop still open |
| 7b reproducibility | `knowledge/audit-reproducibility.md` | Report; published corpus not regenerable without laptop `data/` |
| 7c test hardening | `ingestion/validate_units.py` + analysis/ingestion guards | Code landed; CI tests include regression fixtures |
| 7d logger storage | `knowledge/audit-logger-storage.md` + `scripts/measure_logger_storage.py` | Report only; ~267 MB/day, `pm_orderbook` 75%; no format change |

