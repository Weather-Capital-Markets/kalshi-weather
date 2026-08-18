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
| C Polymarket logger | `ingestion/polymarket_logger.py` | `pm_*` categories; separate `polymarket_heartbeat.sqlite`; `--probe` |

```text
depth_census → validate_emission_forward (when logger has ≥3d raw)
polymarket_logger --probe → --once on VPS alongside Kalshi logger
```

