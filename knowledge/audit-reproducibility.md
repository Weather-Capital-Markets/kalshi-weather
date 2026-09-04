# Reproducibility audit — Session 7b

**Status:** REPORT ONLY — findings document; no code or config changes.  
**Scope:** Whether “files are truth and every load-bearing number carries provenance” holds across `knowledge/`, `plan.md`, and `analysis/out/`.  
**Prerequisite:** Session 7a correctness audit (`knowledge/audit-correctness.md`, branch `cursor/audit-correctness-7a-1b11`).  
**Audit environment:** Cloud agent workspace, 2026-08-21; repo at `main` (`1f59bf1`).  
**Method:** Read `knowledge/data-sources.md`, `knowledge/venue-facts.md`, `knowledge/plan.md`, `ingestion/config.yaml`; grep `[V-LOCAL]` and numeric tables; attempt regeneration commands against workspace `data/` (gitignored).

---

## Executive summary

| Severity | Count | Theme |
|----------|-------|-------|
| CRITICAL | 6 | Historical Kalshi corpus absent from workspace; no commit SHAs on [V-LOCAL] claims; NBM vintage unsettled; spread census never executed |
| HIGH | 12 | `backfill.sqlite` resume without file verification; config latency 60 vs ~435 min; non-reproducible end dates; depth comparison to uncited census |
| MEDIUM | 14 | CLINYC counts drift; `(verify)` tags stale; plan/logger metadata contradictions; analysis outputs gitignored |
| LOW | 8 | Partial reproducibility paths; VPS cadence matches repo; pinned NBM artifact hashes |

**Bottom line:** The repo’s *code paths* are documented, but the *load-bearing Session 2 numbers* in `venue-facts.md` and `plan.md` **cannot be regenerated from a fresh clone** (or from this workspace) because `data/` is gitignored and this environment lacks `markets_history` / `candlesticks` entirely. NBM, window-mismatch, and station-basis numbers are partially regenerable here; spread, turnover, depth, bracket-enumeration, and `validate_candles` PASS claims are not.

---

## 1. Can every published number be regenerated?

### Provenance recording standard (gap)

| What is claimed | What is actually true | What would need to run to close the gap |
|-----------------|----------------------|----------------------------------------|
| `[V-LOCAL]` entries cite **command + capture date** | **No commit SHA, no input tarball hash, no pinned `data/` snapshot** in any knowledge file | Record `git rev-parse HEAD`, `data/` manifest (file counts + SHA-256 of `markets_history` index), and archive path for each [V-LOCAL] block |
| `plan.md` §3 K1 v3 pre-registration date is `____` | Date field **never filled**; v3 validity is not formally ratified in-repo | Root-chat ratification date written into `plan.md`; re-run `validate_candles` under that commit + data snapshot |

### Session 2 — Kalshi bulk backfill & emission validation

| Figure (source) | Documented command | Recorded commit | Input paths | Regenerable today? |
|-----------------|-------------------|-----------------|-------------|-------------------|
| 9,364 markets; span 2021-08-05 → 2026-08-13 (`data-sources.md` §5.6, `plan.md` §4) | `python -m ingestion.kalshi_history --dry-run` (2026-08-14) | **None** (nearest merge: `fd6f4c4`, 2026-08-14) | `data/raw/*/markets_history/*.jsonl.gz` | **No** — workspace has **0** `markets_history` files; `history_markets` table empty |
| 5.25 M candles, 87 MB (`plan.md` §4, `venue-facts.md` §1.7) | `python -m ingestion.kalshi_history` (full backfill) | **None** | `data/raw/*/candlesticks/*.jsonl.gz` | **No** — **0** candlestick files |
| Gap table: modal/median/p90; 22.8% / 22.1% gaps >1m; 3.07% >15m historical (`venue-facts.md` §1.7) | `python -m analysis.validate_candles` (2026-08-14) | **None** | Full candle corpus above | **No** — rerun prints `n_markets=0`, gates **FAIL** |
| Volume reconcile 9,317/9,364; 0.0052% residual (`venue-facts.md` §1.11, `plan.md` §3) | same | **None** | same | **No** |
| 24.6% stale at T−6h live tier (`venue-facts.md` §1.7) | `validate_candles` staleness section | **None** | same | **No** |
| 8,998 / 366 historical vs live split; 96% pre-cutoff (`venue-facts.md` §1.6) | `--dry-run` | **None** | API live (ephemeral) | **Partial** — `--dry-run` is reproducible **if** API unchanged; not pinned |
| Era tables: close-time change 2026-03-17→18; settlement eras (`venue-facts.md` §1.8, §1.10) | `python -m analysis.venue_eras` | **None** | `markets_history` JSONL | **No** — prints `no markets with a parseable ticker date` |
| 1,054 / 774 / 1,829 climate-day close offsets (`venue-facts.md` §1.1, §1.8) | `venue_eras` | **None** | same | **No** |
| 2021 ~1.3 vs ~6 brackets/day (`data-sources.md` §5.7) | `--dry-run` by close year | **None** | `markets_history` | **No** (needs corpus); dry-run alone would re-derive if API stable |

### Session 2 — spread census (primary K1 statistic)

| What is claimed | What is actually true | What would need to run |
|-----------------|----------------------|------------------------|
| Median quoted spread at T−24h (K1 v3, `plan.md` §3) | **`spread_census` has never been executed** (`plan.md` §4: “Census execution — Frozen”) | `validate_candles` OVERALL PASS → `python -m analysis.spread_census`; archive `analysis/out/spread_census.csv` + PNGs with commit SHA |
| `venue-facts.md` §3.3 compares Polymarket depth to “~$14 within 2¢ on KXHIGHNY” from “Kalshi depth census” | **No spread or depth census output exists** in repo or workspace | Run spread census (historical) or depth census (logger); cite output file + date |

### Session 2.5 / 6a — window mismatch

| Figure | Command | Commit | Inputs | Regenerable? |
|--------|---------|--------|--------|--------------|
| Seasonal mismatch rates e.g. JJA LST 2.6% (`window_mismatch.csv`) | `python -m analysis.window_mismatch` | **None** | `data/labels/clinyc.csv`, `climate.cli_time_convention` | **Yes** — rerun on 2026-08-21 reproduced identical CSV (LST/LDT equal when convention unset treats both) |
| K2: ALL cli_outside 7.45% LST / 7.5% LDT; ASOS 9.35%; bracket_disagree 13.82% (`window_mismatch_k2.csv`) | same | **None** | `clinyc.csv`, `data/raw/*/asos_obs/*.jsonl.gz` | **Yes** — reproduced exactly; **but** `bracket_disagree_rate` uses Polymarket ladder not Kalshi (7a H8) |
| Headline refusal when `convention=unknown` | config `climate.cli_time_convention: unknown` | pinned in `config.yaml` | — | **Working as designed** — no single headline rate published |

### Session 4 — station basis

| Figure | Command | Inputs | Regenerable? |
|--------|---------|--------|--------------|
| overall `delta_median=1.0`, `bracket_disagree_frac=0.2337` (`station_basis.csv`) | `python -m analysis.station_basis` | ASOS JSONL; **`end = date.today()`** (line 330) | **Conditionally** — reruns change as calendar advances and ASOS backfill grows |
| JJA `bracket_disagree_frac=0.6155` | same | same | same |
| n_days=2058 overall | same | ASOS through 2026-08-21 | Drifts with `end` and backfill |

### Session 5 — turnover census

| What is claimed | What is actually true | What would need to run |
|-----------------|----------------------|------------------------|
| Headlines: 2022+ medians, brackets-with-volume, zero-volume fraction (`plan.md` §8) | **No numeric headlines committed**; `turnover_census.csv` not in repo | Full `markets_history` + candlesticks → `python -m analysis.turnover_census` |

### Session 6 — NBM & bracket structure

| Figure | Command | Commit | Inputs | Regenerable? |
|--------|---------|--------|--------|--------------|
| 300-day sample, 9 deciles, ~9.4 GB (`plan.md` §9) | `nbm_archive --dry-run` then `nbm_archive` | `a56cc7d` (scope revision) | AWS NOMADS; `data/nbm/decoded/*.parquet` | **Partial** — workspace has **300 parquet + 2701 raw qmd** files matching sqlite; **vintage rule blocked** (latency) |
| Panel A median lag **435.3 min**, p90 **440.9 min** (`nbm_latency_check`) | `python -m analysis.nbm_latency_check` | `3c52152` | HTTP HEAD AWS `.idx` | **Yes** — reproduced 2026-08-21; exits **2 BLOCKING** |
| Panel B `vintage_safe_at_snapshot` **0%** | same | same | same | **Yes** |
| K2 sample: **77/300** days before 2022-12-11 (`nbm_availability_watch --mode report`) | report mode (config-only) | `ed1305f` | `ingestion/config.yaml` `sample_seed: 42` | **Yes** — deterministic from config |
| Bracket enumeration: era-dependent; stable 6-bracket from 2022-12-11 (7a / session notes) | `python -m analysis.bracket_enumeration` | `618c535` | `markets_history` | **No** — `no markets_history data in raw_dir` |
| Pinned NBM artifact hashes (`data-sources.md` §2.1) | manual S3 fetch 2026-01-01 00Z f001 | **None** | S3 objects | **Yes** — hashes are self-contained; re-fetch verifies |

### Session 3 — depth & forward emission

| What is claimed | What is actually true | What would need to run |
|-----------------|----------------------|------------------------|
| Depth medians (Session 3 / plan) | **Never run** — no `depth_census.csv`; workspace has **0** logger `orderbook` JSONL | VPS logger ≥3d → `python -m analysis.depth_census --data-dir data/raw` |
| Forward emission cross-check (K1 v3 obligation) | `validate_emission_forward` always exit 0 (7a H13) | VPS data + live API; fix exit discipline separately |

### Labels — CLINYC

| Figure (`data-sources.md` §1.2) | Command | Regenerable? |
|----------------------------------|---------|--------------|
| 4,913 issuances; 2,413 full-day labels; 1,825/1,829 market days | `python -m ingestion.cli_labels` (2026-08-14) | **Partially** — workspace `clinyc.csv` now **4,926** issuances / **2,422** climate dates (archive grew); counts **drift** with re-fetch |
| 79 months `complete` in `backfill.sqlite` | `cli_month_progress` | sqlite ≠ file manifest — see §2 |

### Polymarket (venue-facts §3)

| Figure | Command | Regenerable? |
|--------|---------|--------------|
| Bracket ladder structure, KLGA/WU settlement | `--probe` 2026-08-18 | **Ephemeral** — live Gamma/CLOB; structure reproducible on demand, not frozen |
| “Order of magnitude deeper” than Kalshi ~$14 | `--probe` + comparison to depth census | **Unclosed** — Kalshi side uncited / nonexistent |

---

## 2. Data provenance (`data/` gitignored)

`.gitignore` excludes `data/`, `*.sqlite`, and `analysis/out/`. A fresh clone has **no truth layer**.

### Module behavior on fresh clone (today)

| Module | Fresh clone behavior | Needs backfill first? | Drifts as data grows? | Pinned? |
|--------|---------------------|----------------------|----------------------|---------|
| `kalshi_history` | Fetches from API; writes `markets_history` + `candlesticks` JSONL; sqlite progress | Yes (hours; ~5.25M candles) | Re-run adds markets after 2026-08-13 | API version not pinned |
| `cli_labels` | Fetches IEM months; rebuilds `clinyc.csv` | Yes (~79 months) | **Yes** — current month never marked complete; counts drift | IEM endpoint in config |
| `asos_obs` | Fetches IEM per station×month | Yes | **Yes** — `end=today` in run loop | UTC month bounds (7a M4) |
| `nbm_archive` | Sampled 300 days → parquet | Yes (~GB) | Sample fixed by `sample_seed: 42` | **Pinned sample**; vintage rule **not** |
| `validate_candles` | Gates census | Markets + candles | N/A | Output depends on corpus vintage |
| `venue_eras` | stdout tables | `markets_history` | New markets change counts | No |
| `spread_census` | empty / error | Full corpus + labels | New markets change all stats | No |
| `turnover_census` | `no markets_history` | Full corpus | Yes | No |
| `depth_census` | `no logger orderbook days` | VPS logger JSONL | **Yes** — logger-length only | No |
| `station_basis` | empty or thin ASOS | ASOS NYC+LGA | **Yes** — `date.today()` end | No |
| `window_mismatch` | runs on labels + ASOS | CLINYC + ASOS | ASOS backfill extends n_days | convention in config |
| `bracket_enumeration` | `no markets_history` | Markets index | New markets change eras | No |
| `nbm_latency_check` | live HTTP HEAD | Network | **Yes** — mirror timestamps move | assumed_latency in config |
| `nbm_availability_watch` | prospective poll | VPS timer | Accumulates CSV over time | `sample_seed` pinned |
| `kalshi_logger` / `polymarket_logger` | VPS daemons | deploy + auth | Continuous accrual | `cadence_sec` in config |
| `validate_emission_forward` | needs logger + API | VPS + network | Per run window | No |

### `backfill.sqlite` vs files on disk (resume integrity)

Progress tables in `ingestion/state.py`: `cli_month_progress`, `asos_month_progress`, `nbm_climate_day_progress`, `candlestick_progress`, `history_markets`.

| What is claimed | What is actually true | What would need to run |
|-----------------|----------------------|------------------------|
| `plan.md` §4: “Bulk fetch — **Done**” | Workspace sqlite: **0** `history_markets`, **0** complete candles; **no** `markets_history` or `candlesticks` under `data/raw/` | Restore laptop tarball or re-run full `kalshi_history`; **reset** sqlite rows if raw missing |
| NBM: 300 days `complete` in sqlite | **300** `data/nbm/decoded/*.parquet` — **consistent** | Good pattern; extend with manifest check on resume |
| CLINYC: 79 months `complete` | `clinyc.csv` exists; raw under `data/raw/2026-08-20/cli_labels/` only (partial daily layout) | `cli_labels` skips completed months — **deleting raw without clearing sqlite skips re-fetch** |
| ASOS: 134 station-months `complete` | Only **~588** JSONL.gz rows under one date folder — **incomplete vs sqlite** | Clear `asos_month_progress` for missing months or add file-existence guard |
| “At least one census run before data existed” | `venue-facts.md` [V-LOCAL] stats dated **2026-08-14** require corpus that **is not in this workspace**; cannot distinguish “census on laptop” vs “docs from dry-run memory” without archived outputs | Ship `data/manifest.json` + `analysis/out/` tarball alongside knowledge updates; or store hashes in knowledge |

**Resume without file verification (7a + reproducibility):**

- `nbm_archive.backfill()` skips when `nbm_day_complete()` — **does not check parquet exists** (`nbm_archive.py` 503–504).
- `cli_labels` / `asos_obs` skip when month complete — **no glob check** that raw JSONL still present.
- `kalshi_history` candle progress can advance on empty 200 payloads (7a H2) — sqlite can show progress without valid JSONL.

---

## 3. Config drift

| Setting | Repo `ingestion/config.yaml` | Ratified / measured | Assessment |
|---------|------------------------------|---------------------|------------|
| `climate.cli_time_convention` | **`unknown`** | Session 2.5: must resolve for Clock B headline | **Deliberate default** per `plan.md`; blocks single headline; spread census uses same |
| `nbm_archive.publication_latency_min` | **60** | `nbm_latency_check` Panel A: **median ~435 min** Last-Modified | **Inconsistent with measurement** — 7a C1: backfill may encode future information |
| `nbm_latency_check.assumed_latency_min` | 60 | same | Hard-stop **fires** (exit 2) — correct gate, wrong latency value |
| `nbm_availability_watch.assumed_latency_min` | 60 | Prospective watch **incomplete** (n=0 first-avail cycles in workspace CSV) | Blocking K2 not closed |
| `nbm_archive.percentile_levels` | `[10..90]` step 10 | K2 scope: 9 deciles not 99 | **Deliberate** subsample — document in any NBM citation |
| `nbm_archive.sample_size` / `sample_seed` | 300 / 42 | 77 pre-2022-12-11 | **Pinned** — reproducible sample list from config |
| `api.orderbook_depth` | **0** | README suggests raising after deploy for depth metrics | **Default** — top-of-book only; depth census uses full JSONL ladder from logger |
| `cadence_sec.orderbook` | 5 | VPS `kalshi-logger.service` uses repo config via `load_config()` | **Consistent** — no drift between `deploy/` and yaml |
| `polymarket.cadence_sec` | markets 300, orderbook 5 | same pattern as Kalshi | **Consistent** |
| `asos_obs.start_date` | 2021-01-01 | Session 4 plan | **Consistent** |
| `station_basis.start_date` | 2021-01-01 | — | **Consistent**; end date **not** in config (uses `today`) |
| `history.series` | KXHIGHNY, HIGHNY | venue-facts: legacy transparent on historical endpoint | **Consistent** |

**VPS vs repo:** `scripts/vps-setup.sh` installs systemd units from `deploy/*.service` with repo path substitution only — **no separate VPS config file**. Logger cadence and depth come from `ingestion/config.yaml` on the VPS checkout.

---

## 4. Knowledge-file integrity

### Provenance tags — `(verify)` / `[REPORTED]` still open

| Claim | Tag / status | Issue |
|-------|--------------|-------|
| NBM archive floor 2020-05-18 | `[REPORTED]` O5 | NBM backfill now exists — tag **stale** |
| NBM text bulletins back to 2020-06-01 | `[REPORTED]` O5 | Unverified |
| `climate.cli_time_convention` | `unknown` in config | data-sources §1.2 TIME column “(LST)” **not** same as resolving Clock B — ASOS cross-check still open |
| NWSI URLs O7 | `[REPORTED]` | Links unverified in-repo |
| 141 climate days settlement snapshot | `venue-facts` V1 / data-sources O9 | **Assumed** 10 AM — not ratified |

### `[V-LOCAL]` claims — settled vs fragile

| Claim | Verdict |
|-------|---------|
| Empty book = 0.00/1.00 (`venue-facts` §1.4) | **Settled** — encoded in `spread_census.py` + tests |
| Historical tier emits on change (`venue-facts` §1.7) | **Settled in code** — **population stats not regenerable** without corpus |
| Weather maker fee $0 (`venue-facts` §2.1) | **Settled** 2026-08-15 — time-sensitive re-check noted |
| Polymarket KLGA / bracket ladder (`venue-facts` §3) | **Settled** — supersedes gateway probe |
| CLINYC backfill counts (`data-sources` §1.2) | **Fragile** — reproducible command but **counts drift**; MM/day coverage needs re-join to markets |
| Pinned grib hashes (`data-sources` §2.1) | **Settled** — hash-pinning is best practice; replicate via S3 |

### Flat claims on single observation

| Claim | Risk |
|-------|------|
| Polymarket “~100 contracts at nearly every penny 3¢–35¢” (`venue-facts` §3.3) | Single `--probe` session 2026-08-18 — no frozen book archive |
| “LaGuardia 1–3°F warmer” (`venue-facts` §3.2) | Climatology [CORR], not project-measured until station_basis cited with snapshot |
| Dry-run 21.3 M candle projection (`venue-facts` §1.7) | Single dry-run — superseded by 5.25 M realized but **realized not in repo** |

### Contradictions / supersessions not fully propagated

| Stale claim | Superseded by | Where still wrong |
|-------------|---------------|-------------------|
| Polymarket same as KNYC NWS settlement | `venue-facts` §3.2 (2026-08-18) | **Fixed** in venue-facts; README still says “gateway” for CLOB host only (OK) |
| Polymarket cumulative `≥X` thresholds | §3.1 bracket ladder | **Fixed** in venue-facts |
| `plan.md` Session 3: `pm_meta` **strike/direction** | `bracket_kind`, `bracket_low_f`, etc. (`polymarket_logger.py`, PR #10) | **`plan.md` line 97** still says `strike/direction` |
| Historical tier sparse emission | `venue-facts` §1.7 revised | Code comments in `spread_census.py` align — **OK** |
| Kalshi depth “~$14 within 2¢” vs depth census | No census output | **`venue-facts` §3.3** cites uncited Kalshi number |
| `validate_candles` OVERALL PASS | Required for census | Workspace rerun **FAIL** — knowledge still states PASS from 2026-08-14 laptop |

---

## 5. Findings (by severity)

### CRITICAL

#### R1 — Session 2 Kalshi corpus not reproducible from clone

- **Claimed:** 9,364 markets, 5.25 M candles, `validate_candles` PASS, era tables (`plan.md` §4, `venue-facts.md` §1).
- **True:** Code exists; **`data/` absent on clone**; this workspace has **zero** `markets_history` / `candlesticks`.
- **Close:** Restore archived `data/` or re-run `kalshi_history`; record SHA + manifest; rerun `validate_candles` and `venue_eras`.

#### R2 — No commit SHA on any [V-LOCAL] figure

- **Claimed:** “Verified by our own reproducible commands; raw output retained.”
- **True:** Only **dates** (mostly 2026-08-14); no `git` SHA, no output tarball, no `data/` hash.
- **Close:** Add provenance block template: `commit`, `python -m …`, `data/manifest.sha256`, `analysis/out/` paths.

#### R3 — Spread census (primary K1 number) never executed

- **Claimed:** Median spread T−24h thresholds in K1 v3 (`plan.md` §3).
- **True:** `plan.md` §4 “Census execution — **Frozen**”; no `spread_census.csv` anywhere.
- **Close:** Ratify v3 date → `validate_candles` PASS → `spread_census` → commit outputs or manifest.

#### R4 — NBM 300-day backfill vintage rule incompatible with measured lag

- **Claimed:** T−24h retrospective ladders (`plan.md` §9).
- **True:** `publication_latency_min: 60` but Panel A **p90 ≈ 441 min**; Panel B **0%** vintage-safe; 7a C1.
- **Close:** Complete `nbm_availability_watch` (6 cycles × 2 sources); update latency; **do not cite** existing parquet until re-run.

#### R5 — `validate_candles` PASS not reproducible here

- **Claimed:** `venue-facts.md` §1.7, §1.11 — gates passed on completed backfill.
- **True:** Rerun 2026-08-21: **OVERALL FAIL** (no data).
- **Close:** Same as R1; store gate stdout with corpus.

#### R6 — `venue-facts` gap / reconcile tables rest on unavailable inputs

- **Claimed:** Full tier gap table and 9,317/9,364 reconcile.
- **True:** Cannot recompute; 7a flags H2/H7 at-risk even if corpus returns.
- **Close:** R1 + optional audit script comparing sqlite progress to JSONL presence.

### HIGH

#### R7 — `backfill.sqlite` marks complete without verifying files exist

- **Claimed:** Resumable backfill (`README.md`).
- **True:** Skip on sqlite flag only (`nbm_day_complete`, `month_complete`, etc.); ASOS sqlite **134** complete vs **~588** raw files in workspace.
- **Close:** On resume, verify expected artifacts; or `manifest check` subcommand.

#### R8 — `station_basis` uses `date.today()` as end

- **Claimed:** Distributions in `station_basis.csv`.
- **True:** `station_basis.py:330` — non-reproducible across dates.
- **Close:** Pin `end_date` in config to ASOS backfill vintage.

#### R9 — CLINYC label counts drift from published [V-LOCAL]

- **Claimed:** 4,913 issuances (2026-08-14).
- **True:** 4,926 issuances now; market-day coverage not re-validated.
- **Close:** Re-run label join; update knowledge with new date + manifest.

#### R10 — `bracket_disagree_rate` in K2 output is not Kalshi

- **Claimed:** Implied K2 bracket disagreement (`window_mismatch_k2.csv`).
- **True:** Uses `polymarket_bracket()` (7a H8); Kalshi structure needs `bracket_enumeration`.
- **Close:** Recompute with Kalshi ladder after R1.

#### R11 — Depth comparison in `venue-facts` §3.3 uncited

- **Claimed:** PM “order of magnitude deeper” than Kalshi ~$14.
- **True:** No Kalshi depth/spread census artifact.
- **Close:** Run spread or depth census; cite row.

#### R12 — K1 v3 ratification date blank

- **Claimed:** v3 supersedes v2 (`plan.md` §3).
- **True:** `date: ____` unfilled; integrity note says drafted after validation seen.
- **Close:** Root-chat date + frozen outputs.

#### R13 — `nbm_availability_watch` incomplete

- **Claimed:** Blocking K2 (plan §10).
- **True:** Workspace CSV mostly `pending`/`pre_existing`; n=0 first-availability samples.
- **Close:** VPS timer through 6 witnessed 404→200 cycles per source.

#### R14 — Analysis outputs gitignored

- **Claimed:** Figures in knowledge / session reports.
- **True:** `analysis/out/` not versioned; only local copies (partial in workspace).
- **Close:** Publish `analysis/out/manifest.json` with SHA-256 per artifact, or attach release tarball.

#### R15 — Turnover census headlines missing

- **Claimed:** Session 5 headlines in `plan.md` §8.
- **True:** Code + tests only; no committed medians.
- **Close:** R1 then `turnover_census`; paste headline row into plan or separate results file.

#### R16 — Era-aware label selection not implemented

- **Claimed:** `data-sources.md` §1.3 settlement-snapshot rule.
- **True:** `cli_labels.rebuild_csv` latest full-day only (7a M1).
- **Close:** Implement era filter; relabel; rerun label-dependent analyses.

#### R17 — Config `publication_latency_min` contradicts measurement

- **Claimed:** 60 min in three config keys.
- **True:** ~435 min Last-Modified; first-avail watch not settled.
- **Close:** Watch completes → set single latency source of truth.

#### R18 — 77/300 NBM sample pre stable bracket era

- **Claimed:** K2 on stable 6-bracket regime.
- **True:** `audit_sample_scope()` prints 77 before 2022-12-11.
- **Close:** Re-sample with `sample_seed` bump or filter; document in knowledge.

### MEDIUM

#### R19 — `plan.md` Polymarket metadata wording stale (`strike/direction` vs `bracket_kind`)

- **Close:** Docs-only fix in a future session (out of scope for 7b code freeze).

#### R20 — `[REPORTED]` NBM floor tags stale post-backfill

- **Close:** Verify O5; upgrade tags.

#### R21 — `window_mismatch` LST/LDT rates identical in CSV when convention unknown

- **True:** Expected for CLI path; SON differs slightly (0.0636 vs 0.0617) — document.

#### R22 — `nbm_latency_check` empty-lag false hard-stop (7a C3)

- **Close:** Correctness fix in future session; reproducibility: keep raw CSV.

#### R23 — VPS forward validation not evidenced in-repo

- **Close:** `kalshi_logger --status` capture in knowledge.

#### R24 — Polymarket probe numbers ephemeral

- **Close:** Freeze `pm_orderbook` JSONL sample in release tarball.

#### R25 — `clockb_check` conclusion not written to `data-sources.md`

- **Claimed:** plan §5 pipeline step.
- **True:** No conclusion paragraph in data-sources.
- **Close:** Run clockb_check; ratify LST/LDT; set config.

#### R26 — Dual README gateway wording vs `clob.polymarket.com`

- **True:** README clarifies CLOB host — low confusion risk.

#### R27 — `asos_obs` UTC month boundaries

- **Close:** 7a M4 — document edge effect in station_basis NOTES.

#### R28 — No `data/manifest.json` or LFS policy

- **Close:** Adopt manifest or DVC for `data/` releases.

#### R29 — Bracket enumeration verdict not in knowledge files

- **Claimed:** Session 6b / 7a “era_dependent; stable from 2022-12-11”.
- **True:** Only in code/tests + watch report; **not** in `venue-facts.md` / `data-sources.md`.
- **Close:** Run `bracket_enumeration` after R1; paste verdict CSV.

#### R30 — Fee schedule screenshot not in repo

- **Claimed:** §2.1 screenshot retained.
- **True:** Not in git.
- **Close:** Store under `knowledge/assets/` or external vault with hash.

#### R31 — Session 2 dry-run projection still cited alongside realized

- **Close:** Keep both but label dry-run as **hypothesis** only.

#### R32 — CI does not guard reproducibility

- **True:** 7a — no frozen corpus integration test.
- **Close:** Optional: smoke test on committed fixture subset.

### LOW

#### R33 — NBM parquet count matches sqlite (300) — good pattern to replicate for other modules

#### R34 — `sample_seed: 42` makes NBM sample list deterministic — document climate dates in knowledge

#### R35 — Window mismatch + station basis reproducible from current partial ASOS/CLI

#### R36 — Pinned grib SHA-256 in `data-sources.md` is exemplary

#### R37 — VPS systemd uses same config yaml as repo

#### R38 — `nbm_availability_watch` report mode is reproducible without network

#### R39 — `audit-correctness.md` on separate branch — merge for traceability

#### R40 — `climate.cli_time_convention: unknown` correctly blocks overconfident headlines

---

## 6. Regeneration matrix (quick reference)

| Output file | Command | Minimum inputs present in workspace? |
|-------------|---------|--------------------------------------|
| `spread_census.csv` | `python -m analysis.spread_census` | **No** |
| `turnover_census.csv` | `python -m analysis.turnover_census` | **No** |
| `depth_census.csv` | `python -m analysis.depth_census --data-dir data/raw` | **No** |
| `bracket_structure.csv` | `python -m analysis.bracket_enumeration` | **No** |
| `validate_candles` stdout | `python -m analysis.validate_candles` | **No** |
| `window_mismatch*.csv` | `python -m analysis.window_mismatch` | **Yes** |
| `station_basis.csv` | `python -m analysis.station_basis` | **Yes** (drifting) |
| `nbm_latency_check.csv` | `python -m analysis.nbm_latency_check` | **Yes** (network) |
| `nbm_availability_watch.csv` | `--mode tick` on VPS | **Partial** |
| `data/nbm/decoded/*.parquet` | `python -m ingestion.nbm_archive` | **Yes** (300 days; vintage caveat) |
| `data/labels/clinyc.csv` | `python -m ingestion.cli_labels` | **Yes** (resumable) |

---

## 7. Recommended closure order (docs/process — not implemented in 7b)

1. **Archive or manifest the Session 2 laptop `data/` snapshot** used for 2026-08-14 [V-LOCAL] claims (markets + candles + sqlite).
2. **Record `git rev-parse HEAD` + manifest hash** beside every knowledge table.
3. **Complete `nbm_availability_watch`** → resolve latency → freeze vintage rule before any NBM citation.
4. **Ratify K1 v3 date** → `validate_candles` PASS → **`spread_census`** → publish `analysis/out/` manifest.
5. **Pin `station_basis.end_date`** and re-run basis + window mismatch bracket column with Kalshi ladder.
6. **Add resume guards**: sqlite complete ⇒ expected files exist (all backfill modules).
7. **Merge Session 7a correctness audit**; track code fixes separately from this reproducibility report.

---

## Document provenance

- Audit session: **7b** (report only).
- Code revision audited: `main` @ `1f59bf1` (2026-08-21).
- Workspace spot-checks: commands rerun 2026-08-21 UTC (see §1 tables).
- No code or config behavior changed in this session.
