# K2-GEFS pre-registration v1

**Status:** recorded 2026-08-30, before any GEFS member temperatures or Murphy
numbers exist. Numeric PASS/FAIL thresholds are read in the root chat; this
file locks the measurement. Scripts print distributions only.

**Question:** Do raw GEFS ensemble members at T−24h carry Murphy resolution
that NBM's blend has smoothed away? K2 failed with NBM RES 0.0050 vs market
RES 0.0164 on 902 T−24h primary-band contracts. Perfect NBM recalibration
cannot close that gap. This is the last untested fair-value avenue.

## Universe

- Same 300 climate days as `data/nbm/decoded_v441/` (seed 43, 2022-12-11 →
  2026-05-03). Day list is read from those parquet stems, not re-drawn.
- Same contracts as `analysis/out/k2_t24_tradable.csv` (K2 T−24h primary band).
- Snapshot: T−24h = climate-day end minus 24 h = 05:00 UTC on climate date D
  (`ingestion.climate_day`, LST = UTC−5 year-round).
- Vintage: latest GEFS cycle whose p90 publication is strictly before the
  snapshot. A cycle publishing at snapshot or later is REJECTED (raise, never
  skip). Latency is **measured**, not assumed. NBM's 60-minute assumption is
  the cautionary tale.

## Source

Operational `noaa-gefs-pds` (HTTPS, no credentials). Reforecast bucket does
not cover this span. 31 members (`gec00` + `gep01`–`gep30`). 0.5° `pgrb2a`.
Byte-range via `.idx` only.

Daily max per member: **max of instantaneous 2 m TMP** at 3-hourly valid times
inside `[D 05:00Z, D+1 05:00Z)`. Accumulating `TMAX` from cycle start (0–N hour
max) is **not** used: it contaminates the climate day with hours before 05Z.

Sub-daily sampling underestimates a continuous max. Expected bias is from
3-hour discretization around the afternoon peak, not the 05Z edges. Report
ASOS hourly-max vs 3-hourly-max on the same 8 hours when ASOS is available.

## Primary statistic

Murphy Brier decomposition (REL, RES, UNC) for GEFS, NBM, and the market on
the **identical joined subset**. Join rate and drop reasons are reported.

Bracket probabilities from members (locked before outcomes):

- Count members whose `round(Tmax_F)` lands in the Kalshi integer bracket
  (same rule as CLI settlement).
- Laplace α=1 over that day's K brackets: `(n_k + 1) / (31 + K)`.
- Not tuned on Brier or RES.

Bootstrap: 2000 draws, seed 43, on `(RES_GEFS − RES_NBM)`, contract-level and
day-clustered.

## Secondaries

- Equal-weight combination: `0.5 p_NBM + 0.5 p_GEFS` per contract, then Murphy.
- RES by season.
- Correlation of ensemble spread (std of member maxes) with `|ensemble mean − CLI high|`.

## Validity

- V1. Vintage p90 publication strictly before snapshot, or the day fails loudly.
- V2. 31 members required; other counts drop the day.
- V3. NBM and market Murphy are recomputed on the GEFS-joined rows, not copied
  from the K2 CSV headline.
- V4. Depth/size is irrelevant here (forecast, not a book).
- V5. Ask-size / trading is out of scope. This session is a resolution test.

## Integrity

Coverage of GEFS publication latency may be written in after the latency probe
and before magnitudes, the way S2 amended V4 after complete-book rates. Murphy
numbers must not exist when thresholds (if any) are dated.
