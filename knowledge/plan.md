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
| B NBM idx parser | `ingestion/nbm_idx.py` | `.idx` line parse, byte ranges, vintage T−24h (60 min latency), era/version tags |
| C NBM archive | `ingestion/nbm_archive.py` | AWS `noaa-nbm-grib2-pds`; range-only via `.idx`; `--probe` / `--dry-run` / resumable backfill; nearest gridpoint for O8 |

```text
window_mismatch (K2 CSV) → nbm_archive --probe (paste output before bulk)
nbm_archive --dry-run → nbm_archive (resumable backfill to data/nbm/decoded)
```

NBM retrospective scope: **300 stratified climate days** (season × v4 early/late sub-era)
with **9 percentile levels** (P10–P90 by 10) of the 18-h max-window ladder — not the full
~1,700-day × 99-level corpus. `nbm_archive --dry-run` calibrates bytes from `.idx` sidecars
only; bulk backfill waits on a sane estimate.

```text
window_mismatch (K2 CSV) → nbm_archive --probe (paste output before bulk)
nbm_archive --dry-run → nbm_archive (resumable backfill to data/nbm/decoded)
```

Eligible span `2021-08-05` → `2026-05-03` (hard cut 2026-05-04). cfgrib +
pyarrow in `requirements-analysis.txt`. Raw `nbm_qmd` JSONL per percentile message.

