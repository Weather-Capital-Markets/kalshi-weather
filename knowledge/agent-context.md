# agent-context.md — Cross-conversation project context digest

**Status:** DERIVED DIGEST — orientation for future agents, **not** a source of truth.
**Captured:** 2026-09-15, by the Cloud Agent env-setup run (`bc-c177956f…c21a`), from the
Cloud Agent conversation transcripts listed in the [Appendix](#appendix--source-conversations).
**Supersession:** This file **never** supersedes anything. On any conflict, the canonical
files win in this order: `gate-decisions.md` (append-only verdicts) → `plan.md` (run order/gates)
→ `venue-facts.md` (venue matters) → `data-sources.md` (weather/label matters) → the project
**root chat**. If a claim here disagrees with those, treat this file as stale and fix it.

**Why this exists:** hard numbers and decisions were scattered across ~26 Cloud Agent
conversations plus the desktop root chat. This consolidates the durable context so a new agent
can orient in one read. Every claim is **[DERIVED]** from transcripts (a claim about what a
conversation said, at a point in time — *not* an independently reproduced fact). Items flagged
`(unverified)` were uncertain even within the source conversation. Re-fetch / recompute before
anything load-bearing rests on a number here.

---

## 1. Project goal & thesis

- **Goal:** determine whether US prediction-market **daily-high-temperature** contracts —
  primarily **Kalshi `KXHIGHNY`** (legacy `HIGHNY`) for NYC, with Polymarket US as a candidate
  second venue — are **miscalibrated in ways that survive real trading costs**. This is a
  hypothesis under test, not an assumption.
- **Method:** sequential **kill tests ordered by uncertainty-killed-per-hour** — "if the answer
  is no, we learn it in weeks."
- **Business shape (fixed premises):** P&L scales with **turnover, not capital** (capital
  recycles daily); **maker-dominant** (taker fee ≈3.5% of premium at 50¢, heaviest in the
  wings); capacity is **book-bound** (grow via more markets, not more size); edge is a
  **12–24 month window**.
- **Two phases:** a **measurement phase** (Sessions 1–7: laptop + VPS ingestion/analysis) →
  a **build phase** (the `wxmm` package: Stage B1 backtest infra, B3 live-as-measurement,
  C1 fair value).
- **Team:** two-person operation — operator (Windows laptop + DigitalOcean VPS) + cofounder.
  The **root chat is the governance authority**; gate verdicts are written there against
  pre-registered numbers.

## 2. Strategy candidates

| # | Strategy | Mechanism | Lives/dies on |
|---|----------|-----------|---------------|
| **S1** | Recalibration | Sell overpriced central brackets / buy underpriced wings using recalibrated **market** prices only; no weather model | K1, K3, K4 |
| **S2** | Relative value / internal consistency | Brackets are exhaustive (Σp=1); trade executable **sum violations** vs ensemble-derived shape | K1, K4; collateral-netting `(verify)` |
| **S3** | Weather-information edge | Quote off NBM-blended probabilities (M0 raw / M1 recalibrated / M2 weather-only / M3 blend) | K1, K2, K4 |
| ~~S2′~~ | (threshold monotonicity) | **WITHDRAWN 2026-08-19** — written against a Polymarket structure that doesn't exist | — |

**Explicitly NOT building:** GraphCast/GenCast, deep nets, satellite CV, thermometer hardware,
cross-venue arb, latency/HFT, RL/LLM agents.

**Verdict so far (see §5):** taker path is dead; S1/S2/S3 as originally framed are dead or
undetermined; the surviving question is whether **uninformed maker spread capture** can win
(→ decided by X1b + K4). "Informed quoting" (a maker-side β≠0 on top of a market anchor) is the
live model form.

## 3. Gates & milestones

Crispest definitions found across conversations. **Verdicts are made only in the root chat**
against pre-registered numbers; `gate-decisions.md` is the append-only record.

| Gate | Question / definition | Status |
|------|-----------------------|--------|
| **K1 — Costs** | "Is the market cheap enough to trade at all?" Spread census over historical candlesticks. **Pre-reg v3** (supersedes voided v2): primary = median quoted spread (ask−bid) at **T−24h**, carry-forward book states, two-sided only (bid≥1¢ & ask≤99¢), mid **10–90¢**, in trading window, **2022+** (2021 separate). **≥4¢ → forecasting thesis killed; 2–4¢ → taker dead, maker live; <2¢ → K2.** | **Executed.** All seasons ≥4¢. Taker path dead. Maker path reframed as **informed quoting**. **Formal A-vs-B K1 verdict never written** — open (see §8). |
| **K2 — Public benchmark** | "Does Kalshi already track the best public forecast inside the spread?" Market-implied dist vs **NBM** 12Z–06Z max-window percentile dist. Brier/CRPS. **Kill:** Kalshi ≈ NBM inside executable spread. | **Executed (Session 6c).** Market **beats** vintage-safe NBM (§5). ⇒ NBM-anchored quoting = adverse selection ⇒ **anchor on the market, model supplies β adjustment**. |
| **K3 — Calibration** | PIT / discrete rank histogram, coverage, CRPS dispersion decomposition on the full implied dist. **Kill:** no economically meaningful dispersion error. | SPECCED, gated on K2. |
| **K4 — Execution** | "Does maker alpha survive real fills + adverse selection?" Live L2 depth; paper fills **last-in-queue** (fill only on full trade-through) → small real capital. **Kill:** apparent edge disappears conditional on fills. | Logger data ACTIVE; needs B3 shadow-fill comparator + months of fills + employer-compliance answer. |
| **Gate 0** | Required combined annual P&L to justify continuing: **$24,000/yr combined** (~$1,000/mo each), gross of tax, **net of venue fees + infra** ⇒ ≈ **$66/day** captured. Keep recurring spend **< ~$50/mo**. | **Not cleared** — needs fills/day × edge/fill (turnover census done; fill share is a K4 quantity). |
| **Root-chat ratification** | Gate verdicts happen **only in the root chat, in writing, against pre-registered numbers**; pre-registration must precede seeing data. | Standing governance. |

**Build-phase stages (the `wxmm` package):**

| Stage | What | Status |
|-------|------|--------|
| **B1** | Backtest instrument: as-of store, backtest clock, exact cost model, era-correct settlement, hedge/basis, snooping-guard ledger + prereg, manual console. **No models/strategies.** | Delivered (PR #28). |
| **B3** | Live trading system **as a measurement device**: live↔replay **byte-identical MarketView** invariant; **shadow-fill comparator** (`wxmm/measure/`) = the K4 instrument; per-order human **send gate**. | Delivered (PR #30). |
| **C1-M1** | Maker-side fair value implementing B3's `FairValueProvider`. Model form: **`logit(p̂_k) = logit(q_k) + β·x_k`**; **null β=0 = pure spread capture** (current best-supported model). | In progress (PR #31); trade-anchor addendum (PR #33); microstructure-only **v0** (PR #34). |
| **C1-X1** | Maker/taker realised returns on `KXHIGHNY` (replicate Bürgi et al. on weather only). **X1b** = is the maker population two-sided or directional (the load-bearing, unpublished part). | Delivered (PR #32). |
| **C1-T1** | Taker "exclusion trade" living in the wings (fee 0.333¢ not 1.750¢). | Specced, not started. |
| **Stage 0 (a.k.a. B2 Phase 3)** | 12-way emission-convention sweep {interval start/end}×{UTC/ET}×{−1,0,+1} to bound carry-forward book-reconstruction error before fitting on reconstructed books. | **NOT RUN (0/12).** Gates the reconstructed-book path (see §8). |

## 4. Session roadmap & status

| Session | Scope | Status / PR |
|---------|-------|-------------|
| 1 | Repo scaffold + production Kalshi market-data logger (VPS-deployable) | Done. Base URL settled to `api.elections.kalshi.com` (logger default `external-api.kalshi.com`). |
| 2 | Historical Kalshi backfill + CLINYC labels + spread census | Done (PR #2). |
| 2.5 | ASOS obs + Clock B calibration; window-mismatch; forward-emission tooling | Done (PR #3); CI added (PR #4). |
| 3 | Depth census (K1 2b) + forward-emission run + Polymarket US logger | Done (PR #9, #10, #12). |
| 4 | KLGA–KNYC station-basis measurement | Done (PR #13). |
| 5 | Turnover census (Gate 0 capacity input) | Done (PR #15). |
| 6a | window_mismatch (settled) + NBM archive ingestion (byte-range) | Done (PR #16, #17). |
| 6b / 6b-fix | K2 prereqs: bracket enumeration + NBM latency (hard-stop fired); prospective availability watch | Done (PR #20, #21). |
| 6c | Forecast-vs-market (the K2 comparison) | Code landed (PR #27); cloud run produced **0 rows** (no Kalshi corpus on cloud). |
| 7a | Correctness audit (report only) → `knowledge/audit-correctness.md` | Done (PR #22). |
| 7b | Reproducibility audit → `knowledge/audit-reproducibility.md` | Done (PR #24). |
| 7c | Test hardening (non-empty asserts, guards, fixtures) — ~178 tests | Done (PR #23). |
| 7d | Logger storage audit (VPS ~13 days headroom) | Done (PR #25). |
| 7e | Correct NBM vintage rule + re-run backfill (unblocks K2) | Done (PR #26). |
| B1/B3/C1 | `wxmm` build phase (§3) | PRs #28–#34; branches stack B1 → B3 → C1-M1 → X1 → v0. |

**Note (research-vs-trading audit):** `plan.md` and `README.md` narration are **stale** relative
to the B1–C1 build phase (README still frames "Session 1 logger only"). The instrument-before-edge
sequencing is coherent; the stale docs are a documentation-debt item, not a contradiction.

## 5. Key findings & numbers (all [DERIVED])

**Kalshi corpus / labels**
- Bulk candlestick backfill: **9,364 markets, 5.25M candles, 87 MB gzipped** (vs a 9.94 GB
  projection — sparsity is emission-on-change). Later file counts cite **9,382** markets
  (unverified whether real drift or snapshot difference).
- CLINYC: **2,413 labels covering 1,825/1,829 market climate days** (4 missing). Market history
  starts **2021-08-05**.
- **Density regime:** ~1.3 markets/day in 2021 vs **~6/day from 2022** — never pool at readout.
- VOLUME_RECONCILE: **9,317/9,364 exact**, residual 0.0052% of volume; 7 over-reconciliations
  (⇒ venue bookkeeping, not capture loss); **47 non-reconciling markets (34 climate days)**
  carried as a robustness column.

**K1 spread census** (2022+, 10–90¢, two-sided, T−24h, carry-forward):

| Season | Median | p25 | p75 | n |
|--------|--------|-----|-----|---|
| DJF | 5.0¢ | 3.0 | 8.0 | 1056 |
| MAM | 4.0¢ | 2.0 | 14.0 | 1192 |
| JJA | 6.0¢ | 3.0 | 22.0 | 1326 |
| SON | 5.0¢ | 3.0 | 14.0 | 989 |

- **Strict-15-min robustness:** MAM & JJA flip to **2¢** ⇒ kill-direction disagreement ⇒ written
  discussion required. Median quote age at T−24h: 4 min (DJF) vs 39 min (JJA); strict15 drops
  ~60% of summer obs. Two-sided share 73–77%; median **3.0 tradeable brackets/day**.

**Turnover census** (2022+, 10–90¢, market-day medians): median premium/market-day **$214**;
~4 brackets with volume; **zero-volume-market-day fraction 22.3%**. Per-climate-day median
premium ~$1,866–2,517 (band-restricted).

**K2** (Session 6c; 300-day stratified sample, seed 43, 2022-12-11→2026-05-03; T−24h n≈902):

| | Brier | Reliability (↓) | Resolution (↑) |
|---|---|---|---|
| NBM ladder | 0.2090 | 0.0114 | 0.0050 |
| Market | 0.1895 | 0.0047 | **0.0164** |

- Market carries **~3.3× resolution, <½ calibration error**, and incorporates info **~3.3×
  faster** (T−24h→T−12h: market +0.0309 vs NBM +0.0094). ⇒ NBM-anchored quoting = adverse
  selection. **GEFS** Laplace resolution (0.0013) does **not** beat NBM; a 0.5/0.5 blend is worse
  than NBM. (unverified) T−24h SON CI flips between contract- vs day-clustered bootstrap.

**S2 relative-value census — FAIL (2026-08-30, `plan.md` §13):** executable MECE basket; T−24h
complete books only **299/1,337**; 77 persistent sell violations; after-fee median slack
**−2.27¢**; buy-set ROC **−12.3%**; ask-size 0% closes the V5 path. Do **not** reuse the 71/300
figure as an S2 denominator (that was NBM-sample mid-sums, not executable 6-leg books).

**NBM latency (the caught leakage bug):** qmd percentile products publish **~435–441 min**
(p90 441, max 453) after nominal cycle — NOT ~30–60 min (that's TEXT bulletins). A 60-min
assumption was wrong ~7× and **voided the original 9.4 GB backfill**. Corrected vintage:
**D−1 12Z ≈ f042**, 441-min availability-safe → `data/nbm/decoded_v441/`; T−12h uses
**D 00Z f030** in a separate store. The void `data/nbm/decoded/` is **kept, not deleted**.

**Bracket structure (era-dependent):** no interior brackets pre-2022-04-27; 2°F bins from
2022-04-27; **stable 6-bracket regime from 2022-12-11** (`STABLE_BRACKET_CUTOFF`; use this for K2).

**Emission cross-check (unresolved, load-bearing):** forward check ≈ **0.9% match, ~2,026 silent
book changes**. The once-quoted "n=225,111" was **confabulated** (= round(2026/0.009)) and is
unreproducible. **Emission-on-change is still `HYPOTHESIS_UNDER_TEST`.** Candles may be
trade-derived vs the logger's quote state ⇒ the comparison may be **ill-posed, not falsified**.

**Station basis** (IEM ASOS, 2021→2026, n=2,057 paired days): delta = round(KLGA)−round(KNYC)
**median +1.0°F; KLGA warmer 54.1% (67.6% JJA)**. Even-edged 2°F bracket disagreement: overall
23.3%, **JJA 61.5%** (only JJA and the middle band are real measurements; DJF 0% is a binning
artifact). IEM-vs-WU provider gap unmeasured ⇒ 23.3% is a **lower bound**. **Hard rule:** Kalshi
NYC (**KNYC/CLINYC**) and Polymarket NYC (**KLGA/Weather Underground**) are **different
underlyings**; naive cross-venue arb is dead; a Polymarket pivot is a **change of underlying**.

**Clock B (LST vs LDT):** measurement-only; EDT days delta_ldt median 48 min vs delta_lst 50 min
(LDT slightly tighter). `climate.cli_time_convention` deliberately left **`unknown`**; no headline
picked. **Window mismatch (K2):** CLI max outside NBM 12Z–06Z window **7.5% overall** (12.9% DJF,
2.4% JJA).

**Polymarket US:** exhaustive disjoint 2°F ladder + tails (≤75, ≥94), **negRisk-linked** (Σp=1,
so S2 transfers); depth ~100 contracts across most ticks. **All PM fee/reward/collateral facts
UNVERIFIED — the cost model must refuse to price PM fills.** PM day convention (WU civil vs CLI
LST) unverified.

**Fees:** Kalshi **maker fee on weather = $0.00** (verified 2026-08-15; re-verify before capital).
Taker = `round_up(0.07·C·P·(1−P))`.

**Motivating literature — Bürgi et al. (CESifo WP 12122):** all-Kalshi post-fee **Maker −11.99%,
Taker −31.46%** (both lose; makers lose ~19.5 pts less; their "makers" are *directional* limit
posters). ⇒ **X1b** (net maker position per bracket-day) decides whether uninformed spread capture
is even viable.

**Kalshi trades API (correction 2026-09-13):** `taker_outcome_side` & `taker_book_side` are
**canonical/required** on live + historical endpoints (`taker_side` deprecated/forbidden). Maker/
taker attribution needs **no book reconstruction**. Assert `yes_price + no_price ≈ 1.00` per trade
(the Kalshi docs example itself violates this — count violations, don't silently reconcile).

## 6. Data inventory & division of labor

| Data | VPS (`logger-1`) | Cloud `/workspace` | Laptop |
|------|------------------|--------------------|--------|
| `orderbook` / `pm_orderbook` raw JSONL | ✓ live capture | — | — |
| `markets_history` / `candlesticks` | ✗ | ✗ | **✓ (only place they live)** |
| NBM `decoded_v441/` | ✗ | ✓ (300 parquets; gitignored) | ✗ (scp or re-run 7e) |
| CLINYC labels | partial | partial | ✓ |

- **VPS = live logging only** (protect its request budget; do not recompact/restart it from an
  analysis agent). **Cloud = code/PRs + NBM backfill.** **Laptop = bulk Kalshi backfill +
  Session-6c run.** This is why the cloud 6c run produced 0 rows.
- VPS: DigitalOcean Ubuntu 24.04; both loggers under systemd since 2026-08-18/19. Storage
  **~267 MB/day** (pm_orderbook ≈ 75%), ~13 days headroom; store-on-change (~83% savings)
  proposed but **not yet implemented**.

## 7. Conventions & constraints (future agents MUST respect)

**Storage / DB isolation**
- **`backfill.sqlite`** = laptop, disposable scratch, single-writer, **no WAL**. **`heartbeat.sqlite`**
  = VPS, live operational evidence. Live-logger code never touches `backfill.sqlite`; backfill code
  never touches `heartbeat.sqlite` (each module's top comment states which DB it may open). NBM uses
  its own `backfill_v441.sqlite` (and a separate T−12h store).
- Raw JSONL is verbatim, append-only, crash-safe (one gzip member per line + fsync); **a heartbeat
  row per HTTP attempt** — a silent gap is the one unacceptable failure mode.

**Time**
- **Climate day = midnight→midnight Local Standard Time year-round (fixed UTC−5)** — i.e.
  1 AM–1 AM during EDT. **Never use `America/New_York` for the day cut** (that reintroduces the DST
  bug). `zoneinfo` only for issuance timestamps / UTC conversion. All capture timestamps UTC ISO-8601.
- `cli_time_convention: unknown` is about **time-of-high** (Clock B), **not** the day boundary; the
  day cut is already LST in both stacks.

**Analysis discipline**
- **Two-sided = bid≥1¢ AND ask≤99¢** (0.00 bid / 1.00 ask = empty book = absence, not a 99¢ spread).
- Verdict-relevant scripts **print distributions only, never pass/fail**; verdicts are root-chat only.
  Analysis CSV headers carry mandatory NOTES; no Gate 0 arithmetic or verdict language in code.
- **Provenance tags** in `knowledge/*.md`: `[V-LOCAL]/[V-PRIMARY] > [CORR] > [REPORTED] > (verify)`;
  date every fee/API/label claim; append-only (never edit a prior claim, supersede it). Files are
  truth; chat recall is not. Hard numbers cross conversations only via `knowledge/` files.
- **Probe → dry-run → bulk** sequencing is binding; paste raw JSON to root chat, not summaries.
- **Assert non-empty outputs** (IEM-422 lesson: a 200-shaped pipeline with an empty CSV is a
  failure; IEM `limit` caps at 9999, 10000 → HTTP 422).
- Any verdict from prospective data before **2026-11-01** is "summer regime" and doesn't clear winter.

**Scope / hosts**
- **Analysis is laptop-only; the VPS is dedicated to logging.** NBM one-shot extraction may borrow
  the VPS (Linux; eccodes installs cleanly). NBM: **byte-range via `.idx` sidecars, never whole
  grib2 files (~283 MB)**.
- Cloud agents cannot run local Windows PowerShell; operator env quirks: inside venv use `python`
  not `python3`; PowerShell scripts need `-ExecutionPolicy Bypass`; `eccodes` needs conda/`ecmwflibs`
  on Windows. Cursor-bundled Python / `PYTHONHOME` can break venvs — use real CPython 3.11+.

**Do-not-rerun / integrity**
- Pre-registration must precede data; record it when a number was set after seeing outputs. **Don't
  delete the old (void) NBM backfill until the new one is confirmed.** Don't re-run a census after
  ratification without written discussion. **A recurring failure pattern (flagged 3×): an extracted
  schema/number is a claim about a page at a point in time, not a fact** — the 441-min NBM latency,
  the 0.9%/n=225,111 confabulation, and the file-06 missing-fields omission are the cautionary cases.

**`wxmm` non-negotiables** (carry into all build stages)
- One time authority; **everything is as-of** (`valid_at` + `available_at`; reads raise
  `LeakageError`; `available_at` = source publication time, never ingestion wall-clock; unknown
  availability = "not yet available"). Backtest clock is the only "now" (no `datetime.now()` in the
  backtest-reachable path, enforced by AST guard). Settlement is point-in-time / era-versioned.
  **Missing ≠ zero.** Two instruments are fungible only if **all four `Underlying` fields** (station,
  product, day convention, revision rule) match. **No auto-execution** (single-use, 60s,
  content-hash-bound human send token). Import-linter contracts enforce layering (strategy ↛ data/
  venues/clock/backtest; decide ↛ execute/live; live ↛ backtest). Live↔replay **byte-identical
  MarketView**. `SIZE_UNKNOWN` and `BOOK_RECONSTRUCTED` are distinct propagating types. Unverified
  facts (PM fees/negRisk/day convention, Kalshi settlement fee) must **fail loudly**, never default.

**`wxmm` architecture holes flagged by the thesis audit** (fix before capital):
- **[time-bomb]** `KALSHI_WEATHER_MAKER_FEE.reverify_after=2026-08-15` expired; `maker_fee` still
  returns $0; only `ops.console.pretrade_blockers` checks it — wire `require_for_capital` into the
  fee/send path.
- `wxmm/hedge` documents "never net KNYC vs KLGA" but `plan_hedge` + `BasisModel` still emit opposite
  size on the other underlying (residual≠0 holds; "never net" does not).
- `decide/engine.py` imports `wxmm.live.ratelimit` and venue `modelled_fee` — ranking is coupled to
  live tokens + venue fees.
- Empty 0/100 book is clipped in `live/feed.py` / the Kalshi adapter, **not** in `build_market_view`
  — the store can yield a fake 99¢ two-sided book.
- Under `NullFairValue`, `propose()` still emits size-1 at-touch quotes (`edge is None`) — "no model"
  ≠ silence (no auto-send, but be aware).

## 8. Open questions / blockers (ranked)

1. **Stage 0 emission-convention sweep — NOT RUN (0/12).** Everything on the reconstructed-book path
   branches on it. Prereg rule: max match ≥0.60 → JOIN_BUG (bound = 1−match); 0.10–0.60 → PARTIAL
   (written adjudication); <0.10 → run a well-posedness hand-audit before declaring "falsified"
   (ill-posed ≠ falsified). Next action: VPS 12-conv sweep on 2026-08-19 → latest complete climate day
   (`analysis/emission_convention_sweep.py`, VPS raw only).
2. **Formal K1 verdict never written** — choose **(A)** ≥4¢ kills S1/S3 incl. informed quoting (only
   pure spread capture survives, unadjudicated by K1) or **(B)** ≥4¢ kills the taker form only, maker
   S3 stays live pending K2. (B) overrides a pre-registered rung after the result was known — must be
   recorded as such in `gate-decisions.md`.
3. **Are Kalshi weather makers the informed side?** (X1b net-position split) — the cheapest
   high-value test; decides whether uninformed two-sided spread capture can win at all.
4. **Gate 0 not cleared** — needs mean daily premium × fill share (fill share is a K4/logger quantity).
5. **K2 decile→bracket interpolation method unreported** — NBM RES=0.0050 is method-sensitive; blocks
   an honest NBM baseline and the `nwp` feature block.
6. **Real-time observation-feed cadence/latency unmeasured** — gates the `obs` feature block and C1-T1;
   `observation/running_max.py` does not exist yet (do not stub).
7. **`cli_time_convention` still `unknown`** despite EDT-day evidence favoring LDT tightness.
8. **Polymarket unknowns:** fee base/units, negRisk collateral mechanics, day convention, and a PM
   label pipeline that **does not exist** (KNYC/CLINYC ≠ KLGA/WU); WU historical cost vs the ~$50/mo
   ceiling.
9. **Collateral netting across brackets (Kalshi)** — open; sets S2 return-on-capital.
10. **Reproducibility debt (7b):** `data/` gitignored; no commit SHAs on `[V-LOCAL]` claims; the spread
    census artifact isn't persisted; several published numbers aren't regenerable on a fresh clone.
11. **Employer-compliance answer** required before any real-money K4. `taker_book_side` YES-frame
    mapping must be **empirically cross-tabbed** before trusting the trade anchor (a sign error inverts
    all P&L).

## 9. `wxmm` build-phase API surface (for build agents)

- **Quote engine:** `propose(view, fair=..., system_status=..., buckets=..., limits=...)`. Default
  `fair = NullFairValue`. FV is a `Mapping[MarketId, Probability]` of `[0,1]` Decimal strings keyed
  by **Kalshi ticker** (e.g. `KXHIGHNY-26JUL04-T90`); today it sets `edge` only (size hard-coded
  `qty=1`; missing FV → `edge=None` but still emits both touch sides). `HALT` → `[]`.
- **MarketView:** sole builder `build_market_view` (`wxmm/backtest/harness.py` on B1, moved to
  `wxmm/core/view.py` on B3); raises on STALE / `AVAILABILITY_UNKNOWN` / future `available_at`.
- **Fair-value anchor:** `wxmm/fairvalue/anchor.py` is **book-mid anchored**; `market_implied(view)`
  → `LadderQuote`; `model.fit()` and `MarketMidBaseline.forecast()` both require a **COMPLETE 12/12**
  Stage-0 sweep via `load_reconstruction_bound` (else `ReconstructionBoundRequired`). The committed
  `analysis/out/emission_convention_sweep.json` is `INCOMPLETE (0/12)` ⇒ fit + market-mid are locked.
  A **trade-derived** anchor should live under `wxmm/fairvalue/` importing from `trades_ingest`, and
  bypasses the reconstruction bound (Stage 0 does not block it).
- **Trades ingest (C1-X1, `cursor/stage-c1-x1-maker-taker-1b11`):** `RawTrade` requires
  `taker_outcome_side` (`yes|no`) + `taker_book_side` (`bid|ask`); `taker_side` forbidden. Mapping:
  `taker_book_side==ask` ⇒ taker lifted offer ⇒ **maker was seller**; `bid` ⇒ **maker was buyer**.
  Historical vs live split at `GET /historical/cutoff`.
- **Measure (K4):** `wxmm/measure/shadow.compare_quote` + `adverse.marks_for_fill`
  (horizons 1m/5m/30m/settlement); `last_in_queue_fill` (behind displayed size; full fill only on
  trade-through; missing size → `SIZE_UNKNOWN`).
- **K2 join keys (Session 6c):** day `ticker_climate_date` ↔ parquet `climate_date`; strike via
  `parse_market_strike` → `ParsedStrike` (interior bins integer 2°F inclusive; CDF with ±0.5°F then
  renormalize); settlement CLINYC `high_F` vs strike → `settled_yes`. Snapshot =
  `climate_day_end − horizon`, `HORIZONS_H=(48,36,24,12,6,3,1)`, `climate_day_end` = midnight LST.
  NBM at `data/nbm/decoded_v441/{climate_date}.parquet` (9 rows/day, `percentile_level` 10…90, use
  `value_f`, vintage D−1 12Z, `forecast_hour` 42, `publication_latency_min` 441).
- **Gates for the trading stack:** `pytest -q`, `ruff check .`, `mypy --strict wxmm`, `lint-imports`;
  B1 leakage canary must go red on peeking; live-vs-replay `MarketView` parity must hold.

---

## Appendix — source conversations

Captured via the Cloud Agent transcript tooling on 2026-09-15. Links are to the agent runs.

| Conversation | bcId | Role in this digest |
|--------------|------|---------------------|
| Kalshi market-data logger (root desktop chat) | [`bc-46cdbcaa…1b11`](https://cursor.com/agents/bc-46cdbcaa-e3ba-423d-b8ac-207a2abe1b11) | Master governance/decision record |
| Summarize desktop agent transcript | [`bc-75ae6ac2…`](https://cursor.com/agents/bc-75ae6ac2-ba94-5341-94df-6d539675a418) | Pre-digested summary |
| Summarize desktop agent transcript | [`bc-1d99c711…`](https://cursor.com/agents/bc-1d99c711-decf-5c62-a241-d92418caab17) | Pre-digested summary |
| Summarize new transcript tail | [`bc-0be8c444…`](https://cursor.com/agents/bc-0be8c444-2da8-5f4b-bb96-6c8994981cd9) | Pre-digested summary |
| Cloud agent research commands | [`bc-563c4693…`](https://cursor.com/agents/bc-563c4693-73d6-4ed0-8f60-9962d2b9e7ec) | S2 FAIL, GEFS, 6c commands |
| Audit research vs trading thesis | [`bc-2da6f08b…`](https://cursor.com/agents/bc-2da6f08b-eeb4-57a6-81b9-6fb818473ace) | Doc-vs-code coherence |
| Audit WXMM layer thesis | [`bc-ec84a834…`](https://cursor.com/agents/bc-ec84a834-a7de-5790-89b8-98f8d200f91c) | `wxmm` invariant holes |
| Decide engine and measure APIs | [`bc-23e3598e…`](https://cursor.com/agents/bc-23e3598e-2fd1-52eb-b5c0-afd2f1b7553e) | `propose()`/measure API |
| Explore fairvalue anchor gate | [`bc-da3ce8de…`](https://cursor.com/agents/bc-da3ce8de-73fa-51ad-b614-dd2e7844601b) | Reconstruction-bound gate |
| Explore C1-X1 trade types | [`bc-af218f90…`](https://cursor.com/agents/bc-af218f90-0c4b-5775-b95d-5ee3293b32a9) | Trade attribution mapping |
| Check B1/B3 preconditions | [`bc-248b6bb4…`](https://cursor.com/agents/bc-248b6bb4-e4b3-505d-bc58-a93631cd4247) | Leakage canary / parity |
| B2 Phase 3 and K2 status | [`bc-e3444246…`](https://cursor.com/agents/bc-e3444246-9dc6-5633-b9aa-50b0cfdd0925) | Stage 0 / K2 join bug |
| Explore K2 join patterns | [`bc-00d21e20…`](https://cursor.com/agents/bc-00d21e20-e324-5430-9af6-379683ebd2cb) | NBM↔Kalshi join keys |
| Explore B1 wxmm surface | [`bc-eb65ba27…`](https://cursor.com/agents/bc-eb65ba27-6667-511b-869f-432df10a2c2e) | B1 package architecture |

**How to maintain:** when a new conversation lands durable context, append its findings here with
an `[DERIVED]` tag and add its row above; promote anything load-bearing into the canonical
`knowledge/` files (with proper provenance) and the root chat. Keep this file an index/orientation,
not a competing source of truth.
