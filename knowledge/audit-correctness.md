# Correctness audit — Session 7a

**Status:** REPORT ONLY — findings as of 2026-08-21 (`main` after PR #21).  
**Remediations since audit (do not rewrite findings):**
- **C1 (NBM vintage):** Session **7e** — empirical D−1 12Z / f042 at p90 441 / max 453 min; backfill in `data/nbm/decoded_v441/`. Finding retained as historical.
- **C2 (CLINYC empty-200):** Session **7c** — `cli_labels` refuses to mark a month complete on empty body; regression test added.
- **C3 (empty-lag hard-stop):** still open in `nbm_latency_check.evaluate_hard_stop`.
**Scope:** All modules in `ingestion/` and `analysis/`.  
**Lens:** `data-sources.md` §7 (instrument failure-modes log).  
**Date:** 2026-08-21.

---

## Methodology

Each module was reviewed for:

1. **Silent failure paths** — plausible output from bad or missing input without raising.
2. **Unasserted assumptions** — schema, units, timezones, ordering not checked at runtime.
3. **Time and vintage** — future-information leakage; day boundaries vs fixed offset vs DST-aware zones.
4. **Test coverage honesty** — real path vs mocks; tests in CI vs orphaned.
5. **Stake assessment** — for result-bearing modules, whether published numbers can be trusted.

**CI baseline:** `.github/workflows/test.yml` runs `pytest -q` on all 31 test files (163 tests at audit time). No `pytest.mark.skip`, no `--ignore`, no excluded analysis tests.

**Integration gap:** No module exercises the frozen production `markets_history` / full `data/raw` corpus end-to-end in CI.

---

## Executive summary

| Severity | Count (unique issues) | Themes |
|----------|----------------------|--------|
| CRITICAL | 3 | NBM vintage unsettled; §7.6 CLINYC empty-200; false NBM hard-stop on empty lags |
| HIGH | 14 | Candle progress gaps; fabricated PM success; spread trading-window default; K2 bracket ladder; nbm_decode untested |
| MEDIUM | 18 | Era label selection; reproducibility; tier split; exit-code discipline |
| LOW | 12 | Logging-only; probe paths; cosmetic |

**Highest-weight risk:** The 300-day NBM backfill may have selected vintages not operable at T−24h under real availability timing. All NBM-derived ladders are blocked until `nbm_availability_watch` completes.

---

## §7 failure-mode repeats

| §7 mode | Repeat in this codebase? | Where |
|---------|--------------------------|-------|
| **1. Sampling grids are hypotheses** | Partial | `nbm_idx` f018+12k grid is correct for window encoding; risk is **config subsampling** (`percentile_levels: [10..90]`) picking levels absent from idx → `empty_ladder`. Not the f012/f024 false-absence trap. |
| **2. Grep encoding, not concept** | **Guarded** | `nbm_idx.IDX_LINE_RE` targets `TMP:2 m above ground:…hour max fcst` — correct contract. |
| **3. Same-day archive listings lie** | **Open** | No intraday guard on `nbm_archive` AWS backfill; `nbm_availability_watch` addresses prospectively. `cli_labels` / `asos_obs` skip marking **current month** complete. |
| **4. Citation links unreliable** | N/A (process) | O7 still open in `data-sources.md`; not code-auditable. |
| **5. Two AIs agreeing ≠ ground truth** | **Yes** | NBM path: 100% mocked HTTP in tests; no pinned artifact hashes in CI. |
| **6. 200-shaped empty pipeline** | **Yes (active)** | `cli_labels.py` — HTTP 200 + empty body marks month complete. Variants: `asos_obs` header-only CSV; `kalshi_history` null candle payload + progress advance; `polymarket_logger` fabricated `http_status=200`. |

---

## Findings (by severity)

### CRITICAL

#### C1 — NBM backfill vintage rule may select future cycles

- **Module:** `ingestion/nbm_archive.py`, `ingestion/nbm_idx.py`
- **Lines:** `nbm_archive.py` 313–321 (`vintage_select_cycle` with `publication_latency_min`); `nbm_idx.py` 234–248
- **What could go wrong:** `publication_latency_min: 60` assumes publication strictly before snapshot. `nbm_latency_check` Panel B found 0% `vintage_safe_at_snapshot` using AWS `Last-Modified`; prospective watch not yet complete.
- **Manifestation:** 300-day backfill (~9.4 GB) may decode percentiles from cycles not available at T−24h — **future information in the retrospective leg**.
- **§7:** §7.3 (mirror timing), §7.5 (latency assumed not measured at backfill time)
- **Could corrupt:** All NBM parquet ladders in `data/nbm/decoded/`; any K2 comparison using those vintages.

#### C2 — §7.6 repeat: CLINYC month marked complete on HTTP 200 + empty body

- **Module:** `ingestion/cli_labels.py`
- **Lines:** 231–254 (`_fetch_month`: checks `result.ok`, not `text.strip()` before `set_month_complete`)
- **What could go wrong:** Same class as historical `limit=10000` → HTTP 422 → empty CSV. Any 200 with empty `text_body` marks month complete and skips forever.
- **Manifestation:** Missing rows in `clinyc.csv` for affected months; spread census label joins degrade silently.
- **§7:** §7.6 explicitly
- **Contrast:** `asos_obs.py` 159–166 blocks empty body — CLINYC path does not.
- **Test gap:** `test_cli_labels.py` documents 422 risk (`test_request_limit_stays_within_what_iem_accepts`) but **does not assert** empty-body must not mark complete.

#### C3 — NBM latency hard-stop fires on empty lag list

- **Module:** `analysis/nbm_latency_check.py`
- **Lines:** 68–70 (`evaluate_hard_stop`: empty `lags` → `hard_stop=True`)
- **What could go wrong:** All HEAD requests fail (network, 403, wrong URL) → exit code 2 BLOCKING with NaN medians — indistinguishable from real 435-min lag.
- **Manifestation:** False hard-stop blocks K2 pipeline; true hard-stop also blocks — operator cannot tell which without inspecting raw CSV.
- **Test gap:** No test for empty-lag path.

---

### HIGH

#### H1 — ASOS month marked complete on header-only CSV

- **Module:** `ingestion/asos_obs.py`
- **Lines:** 159–166 (empty body blocked), 183–184 (`can_complete` after non-empty `text.strip()`)
- **What could go wrong:** IEM returns header row only (`station,valid,tmpf\n`) — passes `strip()` check, zero data rows.
- **Manifestation:** Month skipped in re-fetch; `window_mismatch` / `station_basis` / K2 ASOS leg missing days without hard failure.
- **§7:** §7.6 variant

#### H2 — Candlestick windows advanced on `ok` with null/empty JSON payload

- **Module:** `ingestion/kalshi_history.py`
- **Lines:** 491–515 (`backfill_candles`: writes `result.json_body`, always `set_candle_progress` on ok)
- **What could go wrong:** HTTP 200 with empty or non-list `candlesticks` still advances `last_end_ts` to `window_end`.
- **Manifestation:** Permanent gaps in candle JSONL; `validate_candles` gap stats wrong; spread census stale/missing quotes at horizons.
- **Could corrupt:** `venue-facts.md` gap distributions (modal/median/p90) if any windows were empty-200.

#### H3 — Market enumeration pagination breaks silently

- **Module:** `ingestion/kalshi_history.py`
- **Lines:** 162–168 (`break` on failed page), 563–564 (`persist_market_index` on partial `enumerate_markets`)
- **What could go wrong:** Mid-pagination failure returns partial market list without raising.
- **Manifestation:** Incomplete `markets_history` JSONL persisted as if complete; censuses under-count markets/days.

#### H4 — Polymarket markets logged with fabricated HTTP 200

- **Module:** `ingestion/polymarket_logger.py`
- **Lines:** 169–175 (`poll_markets`: `http_status=200`, `latency_ms=0` on Gamma-derived payload)
- **What could go wrong:** Gamma discovery failure still leaves stale ladder from sqlite; new write looks successful.
- **Manifestation:** `pm_markets` JSONL cannot be used to audit Gamma fetch health; heartbeat misleading.
- **§7:** §7.6 (success-shaped record without verified upstream)

#### H5 — Settlement marked captured on any `result.ok` without status check

- **Module:** `ingestion/kalshi_logger.py`
- **Lines:** 278–294 (`poll_settlement`: `mark_settlement_captured` when `result.ok`)
- **What could go wrong:** Market still open/settling returns 200 → never retried.
- **Manifestation:** Missing final settlement fields in `markets` captures.

#### H6 — Spread census: missing open/close → in trading window

- **Module:** `analysis/spread_census.py`
- **Lines:** 330–334 (`in_window = True` when `open_dt` or `close_dt` is None)
- **What could go wrong:** Markets without metadata timestamps counted as tradeable at T−24h…T−1h.
- **Manifestation:** Inflated quote coverage; depressed spread medians; horizons computed when market may not have existed.
- **Stake:** Primary T−24h spread statistic if run without `validate_candles` gate.

#### H7 — Spread census: main path does not tier-dedup candles

- **Module:** `analysis/spread_census.py`
- **Lines:** 280–296 (`load_candles` merges all tiers); 620–631 (`load_candles_by_tier` only for `_exclnoreconcile`)
- **What could go wrong:** Dual-tier tickers may mix live and historical candle streams in primary stats.
- **Manifestation:** Quote at horizon may come from wrong tier; exclusion set uses different loader than primary.

#### H8 — K2 `bracket_disagree` uses Polymarket ladder, not Kalshi

- **Module:** `analysis/window_mismatch.py`
- **Lines:** 115–118 (`polymarket_bracket` on CLI vs ASOS whole-°F)
- **What could go wrong:** Kalshi bracket structure is era-dependent (`bracket_enumeration`: sparse pre-2022, 6-bracket stable from 2022-12-11). Polymarket even-edged ladder differs.
- **Manifestation:** `bracket_disagree_rate` in `window_mismatch_k2.csv` is **not** Kalshi bracket disagreement.
- **Sibling:** `station_basis.py` 48–55 — same `polymarket_bracket()` for `bracket_disagree_frac`.

#### H9 — `nbm_decode.py` untested; hardcoded Kelvin and first data_var

- **Module:** `ingestion/nbm_decode.py`
- **Lines:** 24–27 (ImportError → empty datasets); 70, 136–140 (`kelvin_to_fahrenheit`; `next(iter(ds.data_vars))`)
- **What could go wrong:** Wrong unit if msg not Kelvin; wrong field if multiple vars; cfgrib missing → `None` ladder entries without distinguishing missing dep vs missing data.
- **Manifestation:** Wrong °F in parquet ladders; partial ladders marked `partial_ladder` or wrong values in `ok` days.
- **Tests:** Zero direct tests; always mocked in `test_nbm_archive.py`.

#### H10 — NBM archive per-level failures continue silently

- **Module:** `ingestion/nbm_archive.py`
- **Lines:** 286–300 (`continue` on fetch/decode failure per byte range)
- **What could go wrong:** Subset of percentile levels dropped; day may still reach `status=ok` if `len(ladder) >= expected_levels` without verifying correct percentile identities.
- **Manifestation:** Wrong or duplicated levels in ladder; hard to detect without level-by-level audit.

#### H11 — Depth census drops missing logger snapshots without coverage metric

- **Module:** `analysis/depth_census.py`
- **Lines:** 151–156 (`continue` when no capture at horizon); 250–254 (empty → exit 0)
- **What could go wrong:** Missing VPS data looks like zero depth / illiquidity.
- **Manifestation:** Depth medians biased; no row-level "missing logger" flag in output aggregates.

#### H12 — Depth census computes `logger_age_sec` but never uses it

- **Module:** `analysis/depth_census.py`
- **Lines:** 172 (`logger_age_sec` in row), 154–156 (no staleness gate unlike `spread_census.is_stale`)
- **What could go wrong:** Stale orderbook capture treated as valid snapshot.
- **Manifestation:** Depth at horizon reflects old book, not book at snapshot.

#### H13 — `validate_emission_forward` always exits 0

- **Module:** `analysis/validate_emission_forward.py`
- **Lines:** 247–259 (SHORTFALL notes), 335 (`return 0`)
- **What could go wrong:** Insufficient days/markets, API failures, mismatches — all return success exit code.
- **Manifestation:** Pipeline cannot gate on this script; SHORTFALL only in stdout.

#### H14 — `validate_emission_forward` API failure → empty candles silently

- **Module:** `analysis/validate_emission_forward.py`
- **Lines:** 132–134 (`fetch_candles` returns `[]` on failure)
- **What could go wrong:** Match rate computed on subset; silent emission undercounted.
- **Manifestation:** False PASS on thin evidence.

---

### MEDIUM

#### M1 — CLI labels: no era-aware settlement issuance selection

- **Module:** `ingestion/cli_labels.py`
- **Lines:** 256–277 (`rebuild_csv` takes latest parsed row per product, no snapshot-hour filter)
- **What could go wrong:** `data-sources.md` O9 — pre-2024-09-04 markets need 10 AM snapshot; post-era 7/8 AM. Single parser path does not branch.
- **Manifestation:** Wrong `high_F` label for era-mismatched days; spread census label join wrong silently.

#### M2 — CLI labels: truncated month at IEM limit still marked complete

- **Module:** `ingestion/cli_labels.py`
- **Lines:** 236–241 (warn at `IEM_MAX_LIMIT`), 253–254 (`set_month_complete` regardless)
- **Manifestation:** Partial month in archive; labels missing for late-month issuances.

#### M3 — CLI labels: non-modern products skipped silently

- **Module:** `ingestion/cli_labels.py`
- **Lines:** 104–138 (`parse_modern_product` returns None); 267–270 (`skipped` counter only in log)
- **Manifestation:** Pre-modern format invisible in CSV (acceptable for KXHIGHNY span if all modern — not verified for every row).

#### M4 — ASOS month bounds in UTC, not LST climate-day

- **Module:** `ingestion/asos_obs.py`
- **Lines:** 129–130 (`sts`/`ets` month boundaries UTC)
- **Manifestation:** ~5h edge mis-boundary at month edges for LST daily max alignment.

#### M5 — Station basis: `end = date.today()` — non-reproducible

- **Module:** `analysis/station_basis.py`
- **Lines:** 330
- **Manifestation:** Re-run on different calendar date extends/changes CSV and PNGs.

#### M6 — Station basis: IEM ASOS ≠ Polymarket WU settlement

- **Module:** `analysis/station_basis.py`
- **Lines:** 28–33 (`NOTES_CAVEAT` printed); 48–55 (Polymarket bracket on IEM data)
- **Manifestation:** `bracket_disagree_frac` is PM-ladder on IEM temps — documented lower bound, not cross-venue truth.

#### M7 — Turnover: climate-day share = mean of per-bracket shares

- **Module:** `analysis/turnover_census.py`
- **Lines:** 287–289 (documented aggregation)
- **Manifestation:** `share_vol_last_*` at climate-day level can diverge from pooled-volume share.

#### M8 — Turnover: unparseable ticker skipped

- **Module:** `analysis/turnover_census.py`
- **Lines:** 224–226
- **Manifestation:** Under-counted markets in turnover table.

#### M9 — Window mismatch: days dropped without summary count

- **Module:** `analysis/window_mismatch.py`
- **Lines:** 102–107 (K2: missing `time_of_high_raw` or ASOS → `continue`)
- **Manifestation:** Rates computed on subset; n_days in output may not reflect dropped days prominently.

#### M10 — Window mismatch: refuses headline when convention unknown (good) but K2 still prints bracket rate

- **Module:** `analysis/window_mismatch.py`
- **Lines:** 266–267 (primary path); 115–118 (K2 bracket uses PM ladder regardless)
- **Manifestation:** Dual LST/LDT CLI rates reliable; `bracket_disagree_rate` unreliable for Kalshi.

#### M11 — `nbm_availability_watch`: first poll already 200 → `pre_existing`

- **Module:** `analysis/nbm_availability_watch.py`
- **Lines:** 311–322
- **Manifestation:** Correct for avoiding false first-avail; timer must be running before publication.

#### M12 — `nbm_availability_watch`: `run_blocking` exits 0 if deadline missed

- **Module:** `analysis/nbm_availability_watch.py`
- **Lines:** 395–441
- **Manifestation:** Incomplete watch looks like success in exit code.

#### M13 — Kalshi client: 2xx non-JSON → `ok=True`, `json_body=None`

- **Module:** `ingestion/client.py`
- **Lines:** 191–199
- **Manifestation:** Downstream writes empty payload with success status.

#### M14 — Polymarket gamma: event selection by `endDate` not climate date

- **Module:** `ingestion/polymarket_gamma.py`
- **Lines:** 174–200 (`pick_current_and_next_events`)
- **Manifestation:** Wrong active event near timezone boundaries.

#### M15 — Polymarket gamma: `yes_token_id` = first CLOB token

- **Module:** `ingestion/polymarket_gamma.py`
- **Lines:** 269–280
- **Manifestation:** Orderbook may track No token.

#### M16 — Config NBM percentile subsample

- **Module:** `ingestion/config.yaml`
- **Lines:** 96–97 (`percentile_levels: [10,20,…,90]`)
- **Manifestation:** Intended for K2 scope; wrong if idx era lacks those exact tails.

#### M17 — NBM archive `ts_utc` on write is fetch time, not snapshot time

- **Module:** `ingestion/nbm_archive.py`
- **Lines:** 368 (`utc_now_iso()` on parquet write)
- **Manifestation:** Metadata does not record point-in-time retrieval instant (minor for static archive).

#### M18 — Multiple analysis modules exit 0 on empty input

- **Modules:** `depth_census.py` 254, 271; `turnover_census.py` 558; `station_basis.py` 354; `window_mismatch.py` 237, 241, 264; `spread_census.py` 657
- **Manifestation:** Shell pipelines treat empty output as success.

---

### LOW

#### L1 — `writer.py` writes any payload regardless of caller discipline

- **Lines:** 37–70 — by design; poisoned JSONL if caller wrong.

#### L2 — `state.py` ASOS migration assumes `station='NYC'` for legacy rows

- **Lines:** 233–237 — LGA pre-migration mis-attribution.

#### L3 — `heartbeat.py` string compare on ISO timestamps

- **Lines:** 89–92 — fragile if format drifts.

#### L4 — `kalshi_logger` markets pagination failure leaves stale tickers

- **Lines:** 164–180 — orderbook polls wrong set.

#### L5 — `polymarket_logger.probe` returns 0 when no event

- **Lines:** 278–280 — probe success on failure.

#### L6 — `bracket_enumeration`: unparsed markets still in `n_brackets`

- **Lines:** 212–214, 247 — bracket count inflation.

#### L7 — `clockb_check` / `venue_eras` — drop days without ASOS or on DST transition

- **Documented** — reduces n for those analyses.

#### L8 — `orderbook.py` returns None for one-sided book — callers must handle.

#### L9 — `nbm_idx` `candidate_cycles_for_snapshot` 48h window only

- **Lines:** 251–262 — edge case if snapshot logic changes.

#### L10 — `config_loader` missing keys fail at runtime not load time

- **Lines:** 15–19.

#### L11 — `polymarket_client.py` no tests

- Unused directly by logger tests in some paths.

#### L12 — `cli_labels.rebuild_csv` zero rows no error

- **Lines:** 256–277 — empty CSV written.

---

## Could have corrupted a reported number

Explicit mapping from **V-LOCAL / plan.md / venue-facts.md** claims to audit findings.

| Reported claim | Source | Risk finding | Assessment |
|----------------|--------|--------------|------------|
| Volume reconcile 9,317/9,364 exact; 0.0052% residual | `venue-facts.md` §1.11 | H2 candle progress gaps | **At risk** if any 200-empty windows during bulk fetch |
| Gap distributions (modal/median/p90; gaps >1m / >15m) | `venue-facts.md` table §1.11 | H2, H7 | **At risk** for same reason; tier split adds secondary risk |
| `validate_candles` OVERALL PASS | `venue-facts.md` §1.11 | Gate logic sound; input completeness depends on H2 |
| 9,364 markets; date span 2021-08-05 → 2026-08-13 | `data-sources.md` §5.1 | H3 partial enumeration | **Low risk** if dry-run completed without pagination break |
| NBM mirror lag median ~435 min; Panel B 0% safe | Session 6b / `nbm_latency_check` | C1, C3; Last-Modified ≠ first availability | **Metric measures wrong quantity** — not yet corrected by watch |
| 300-day NBM backfill 9.4 GB; 00Z/f030 vintage | Cloud agent / plan §9 | C1 | **High risk** — vintage unsettled |
| Bracket enumeration `era_dependent`; stable 6-bracket from 2022-12-11 | Session 6b | L6 unparsed count | **Likely sound** for structure verdict |
| 77/300 NBM sample before 2022-12-11 | `nbm_availability_watch` audit | Sample scope, not arithmetic | **Scope issue** for K2, not corruption |
| 2021 ~1.3 vs ~6 brackets/day | `data-sources.md` §5.1 | `spread_census` era column | **Likely sound** |
| Station basis KLGA−KNYC delta distributions | Session 4 / plan §7 | M5, M6, H8 for bracket columns | **Conditional** on IEM-only interpretation |
| Window mismatch outside-window rates | Session 6a | M9, M10; convention unknown | **Conditional** — LST/LDT dual rates, no single headline |
| K2 `bracket_disagree_rate` | `window_mismatch_k2.csv` | H8 | **Not valid for Kalshi** |
| Depth census medians | Session 3 | H11, H12; logger-length only | **Do not cite** until coverage + staleness fixed |
| Turnover medians 2022+ 10–90¢ | Session 5 / plan §8 | M7, M8 | **Conditional yes** for Kalshi contract/premium |
| Forward emission validation | Session 3 | H13, H14 | **Not staked** — exit 0 always |

---

## Result-bearing module stake table

| Module | Stake published numbers? | Why / why not |
|--------|--------------------------|---------------|
| `spread_census.py` | **Conditional** | Mechanics (A6 empty book, staleness, exclnoreconcile) well-tested. **Not** for Clock B regime splits until LST/LDT verified. **Not** if `validate_candles` gate failed or H6/H7 apply. |
| `depth_census.py` | **No** | Logger-length only; H11–H12; one substantive test; exit 0 on empty. |
| `turnover_census.py` | **Conditional** | Kalshi historical medians by era/season credible. Climate-day share aggregation (M7) caveat. Maker capture explicitly upper bound. |
| `station_basis.py` | **Conditional** | IEM paired-station delta on fixture + DST test OK. **Not** for `bracket_disagree` (H8/M6). **Not** reproducible (M5). |
| `window_mismatch.py` | **Conditional** | Outside-window rates with dual LST/LDT hypotheses — diagnostic only. **Not** K2 `bracket_disagree_rate` (H8). |
| `validate_emission_forward.py` | **No** | H13–H14; API always mocked in CI. |
| `nbm_archive.py` | **No** | C1 blocks all NBM ladder citations until availability watch completes. |
| `bracket_enumeration.py` | **Yes** | Metadata-derived structure; tests cover parsing and verdict logic. |
| `nbm_latency_check.py` | **No until live** | C3 false stop; measures Last-Modified not first availability. |
| `nbm_availability_watch.py` | **No** | Prospective; incomplete by design (M11–M12). |
| `validate_candles.py` | **Yes (gate)** | Must PASS before spread census primary; returns 1 on gate fail. Input quality depends on H2. |
| `venue_eras.py` | **Yes** | Changeover dates for settlement-era analysis; tested. |
| `clockb_check.py` | **Conditional** | Diagnostic; ASOS-dependent drops. |

---

## Prerequisite chain (Session 6b → K2)

```
nbm_availability_watch (prospective, blocking)
    → settles vintage rule vs Last-Modified artifact
nbm_latency_check (retrospective HEAD, Panel B)
    → hard-stop on mirror metadata (may be wrong quantity)
nbm_archive (300-day backfill — DO NOT RE-RUN until watch completes)
bracket_enumeration → sample ≥ 2022-12-11 (drop 77 days)
window_mismatch K2 → fix bracket ladder before citing bracket_disagree
spread_census → requires validate_candles PASS
```

---

## Test coverage matrix

| Module | Test file | In CI | Mocks HTTP? | Would catch realistic regression? |
|--------|-----------|-------|-------------|-----------------------------------|
| `client.py` | `test_client.py` | Yes | MockTransport | Retry logic only |
| `cli_labels.py` | `test_cli_labels.py` | Yes | Yes | Parser yes; **C2 empty-body gap** |
| `asos_obs.py` | `test_asos_obs.py` | Yes | Yes | Empty body yes; **H1 header-only gap** |
| `asos_parse.py` | `test_asos_parse.py` | Yes | Fixture | Parse only |
| `kalshi_history.py` | `test_kalshi_history.py` | Yes | ScriptedClient | Cutoff routing; **not H2 gaps** |
| `kalshi_logger.py` | `test_kalshi_logger.py` | Yes | MockKalshiClient | Cursor order; **not H5 settlement** |
| `nbm_archive.py` | `test_nbm_archive.py` | Yes | HTTP+decode mocked | **No live NOAA path** |
| `nbm_decode.py` | — | **No** | Always mocked | **No** |
| `nbm_idx.py` | `test_nbm_idx.py` | Yes | Fixture idx | Vintage math yes |
| `nbm_grid.py` | `test_nbm_grid.py` | Yes | No | Yes |
| `spread_census.py` | `test_spread_census.py` | Yes | Synthetic JSONL | Unit mechanics yes; **no frozen corpus** |
| `depth_census.py` | `test_depth_census.py` | Yes | 1 synthetic OB | **Minimal** |
| `turnover_census.py` | `test_turnover_census.py` | Yes | Synthetic | Good unit coverage |
| `station_basis.py` | `test_station_basis.py` | Yes | Small fixture | May 2025 slice only |
| `window_mismatch.py` | `test_window_mismatch.py` | Yes | Inline CSV strings | K2 bracket not vs Kalshi |
| `validate_candles.py` | `test_validate_candles.py` | Yes | Synthetic | Gate logic yes |
| `validate_emission_forward.py` | `test_validate_emission_forward.py` | Yes | MagicMock API | **Not live API** |
| `nbm_latency_check.py` | `test_nbm_latency_check.py` | Yes | Panels mocked | **C3 gap** |
| `nbm_availability_watch.py` | `test_nbm_availability_watch.py` | Yes | Mocked | Poll arithmetic yes |
| `bracket_enumeration.py` | `test_bracket_enumeration.py` | Yes | Synthetic | Structure yes |
| `polymarket_*` | respective tests | Yes | Mocked | Logger/Gamma/CLOB only |
| `state.py`, `heartbeat.py`, `writer.py` | yes | Yes | sqlite/fs | Partial |
| `config_loader.py` | — | No | — | — |
| `polymarket_client.py` | — | No | — | — |
| `climate_day.py`, `climate_time.py` | yes | Yes | No | Good |

**`candles_by_tier` note:** `validate_candles.load_candles_by_tier` is tested in `test_validate_candles.py` and used in spread census exclusion path — not the primary `load_candles` path (H7).

---

## Module inventory (condensed)

### `ingestion/` (21 modules)

| Module | Silent | Assumptions | Time/vintage | Tests |
|--------|--------|-------------|--------------|-------|
| `__init__.py` | — | — | — | — |
| `client.py` | L13 | Kalshi JSON | — | Mocked |
| `config_loader.py` | L10 | YAML shape | — | None |
| `state.py` | L2, L3 | sqlite schema | — | Real sqlite |
| `writer.py` | L1 | ts_utc format | UTC dates | Real fs |
| `heartbeat.py` | L3 | ISO strings | — | Real sqlite |
| `cli_labels.py` | **C2**, M2–M3, L12 | °F, modern-only | Issuance UTC; **no era snapshot** | Mocked; C2 gap |
| `climate_day.py` | — | LST = UTC-5 | Fixed offset | Unit |
| `climate_time.py` | M implicit | convention unknown | NBM window UTC | Unit |
| `asos_obs.py` | **H1**, M4 | tmpf °F, tz=UTC | UTC month bounds | Mocked |
| `asos_parse.py` | skip rows | valid format | — | Fixture |
| `kalshi_history.py` | **H2–H3** | API shapes | cutoff routing | Mocked |
| `kalshi_logger.py` | **H5**, L4 | dollars | — | Mocked |
| `polymarket_client.py` | L13 sibling | JSON | — | **None** |
| `polymarket_clob.py` | non-JSON 2xx | prob vs cents | — | Mocked |
| `polymarket_gamma.py` | M14–M15 | °F brackets | endDate sort | Mock + unit |
| `polymarket_logger.py` | **H4**, L5 | — | — | Heavy mock |
| `nbm_idx.py` | strict regex | cycle/f-hour rules | vintage math | Fixture |
| `nbm_archive.py` | **C1**, H10, M16–M17 | latency 60m, gridpoint | T-24h snapshot | Mocked |
| `nbm_decode.py` | **H9** | **Kelvin always** | — | **None** |
| `nbm_grid.py` | raises on bad mesh | lon norm | — | Unit |

### `analysis/` (14 modules)

| Module | Silent | Assumptions | Time/vintage | Tests |
|--------|--------|-------------|--------------|-------|
| `__init__.py` | — | — | — | — |
| `orderbook.py` | None returns | fp ladders | — | Via census tests |
| `spread_census.py` | **H6–H7**, M18 | dollars, A6 | LST horizons; Clock B open | Synthetic |
| `depth_census.py` | **H11–H12**, M18 | 10–90¢ band | logger-length | 1 test |
| `turnover_census.py` | M7–M8, M18 | premium=vol×mid | close_time buckets | Synthetic |
| `station_basis.py` | M5–M6, M18 | IEM, PM ladder | LST daily max | Fixture |
| `window_mismatch.py` | M9–M10, M18 | NBM 12Z–06Z UTC | LST/LDT dual | Inline CSV |
| `validate_emission_forward.py` | **H13–H14** | ±0.005 match | minute bounds | Mocked API |
| `validate_candles.py` | gate fail → 1 | tier dedup | — | Synthetic |
| `clockb_check.py` | drop no ASOS | LST/LDT | — | Tests exist |
| `venue_eras.py` | DST skip | contract text | settlement eras | Tests exist |
| `bracket_enumeration.py` | L6 | strike metadata | start_date | Synthetic |
| `nbm_latency_check.py` | **C3** | Last-Modified | Panel B snapshot | Mocked |
| `nbm_availability_watch.py` | M11–M12 | first-200 wall clock | prospective | Mocked poll |

---

## Recommended verification before citing numbers (Session 7b input)

1. Complete `nbm_availability_watch` on VPS (6 witnessed 404→200 cycles per source); reconcile with latency check.
2. Do **not** cite NBM backfill ladders or re-run archive until vintage rule is corrected.
3. Run `validate_candles` on bulk fetch; require OVERALL PASS before spread census primary.
4. Relabel or recompute K2 `bracket_disagree` using Kalshi logic from `bracket_enumeration`.
5. Add regression test: `cli_labels` 200 + empty body must not mark month complete (C2).
6. Pin `station_basis` end date to data vintage for reproducibility.
7. Restrict K2 NBM sample to ≥ 2022-12-11 (drop or re-draw 77 days).

---

## Document provenance

- Audit session: **7a** (report only).
- Code revision: `main` after merge of PR #21 (`nbm_availability_watch`).
- No code behavior changed in this session.
