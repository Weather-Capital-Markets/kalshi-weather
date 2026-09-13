# Kalshi — Get Historical Trades (REST)
SOURCE: https://docs.kalshi.com/api-reference/historical/get-historical-trades
RETRIEVED: 2026-08-14
STATUS: **CORRECTED 2026-09-13 — see correction block below. The original response-field list was incomplete.**
TIME-SENSITIVE: yes (API surface changes)

---

## ⚠ CORRECTION — 2026-09-13, re-fetched from the same primary source

**The 2026-08-14 extraction of the response schema omitted the taker-side fields. It is incomplete,
not merely dated.** The response object on **both** the live (`GET /markets/trades`) and historical
(`GET /historical/trades`) endpoints includes, verbatim:

| Field | Type | Notes |
|---|---|---|
| `taker_outcome_side` | string, enum `"yes"` / `"no"` | Outcome direction the taker is positioned for. **Canonical.** Documented as required. |
| `taker_book_side` | BookSide, enum `"bid"` / `"ask"` | Book side equivalent to `taker_outcome_side`. **Canonical.** Documented as required. |
| `taker_side` | string, enum `"yes"` / `"no"` | **DEPRECATED** — the docs direct users to `taker_outcome_side`. Do not use. |

Docs note, quoted: *"taker_outcome_side and taker_book_side will become the canonical way to
determine trade direction."*

**Consequence, and it is large.** Kalshi's public trade record identifies the aggressor directly.
Maker/taker attribution requires **no** Lee-Ready-style classification, **no** order-book
reconstruction, and is therefore **not affected** by the unresolved emission cross-check or by the
`[Dub26]` finding that feed-inferred trade direction is unreliable in prediction markets. Any
maker/taker analysis on `KXHIGHNY` runs on the full trade history from the trade record alone.

This corroborates `[Bur26]` (CESifo WP 12122), which states: *"A distinctive feature of Kalshi's
data is that it records directly which side of each trade was initiated by the Maker and which by
the Taker, so we can compare Makers and Takers without relying on noisy trade-classification
algorithms,"* and used it across 2021 to April 2025.

**Implementation trap in the docs example.** The example shows `yes_price_dollars: "0.5600"` and
`no_price_dollars: "0.5600"`, which sum to 1.12. That is placeholder text, not a real trade. Code
must **assert** `yes_price + no_price ≈ 1.00` per trade and raise on violation rather than assume
the identity holds silently.

**Side mapping, stated once so it is not re-derived per use:** `taker_book_side == "ask"` means the
taker lifted a resting offer, so the maker was the **seller** at that price. `taker_book_side ==
"bid"` means the taker hit a resting bid, so the maker was the **buyer**. `taker_outcome_side` gives
which contract the taker ended up long; the maker holds the opposite outcome at the complementary
price.

`(verify)`: archive depth for the historical endpoint. The page states only that trades filled
before the historical cutoff are available here and defers to the Historical Data page. Read
`GET /historical/cutoff` before any backfill, per `04-kalshi-historical-data.md`.

---

## Endpoint
- `GET /trade-api/v2/historical/trades`
- Full URL: `https://external-api.kalshi.com/trade-api/v2/historical/trades`
- Example request: `https://external-api.kalshi.com/trade-api/v2/historical/trades?limit=100`

## Query parameters (verbatim)
| Parameter | Type | Description | Defaults / constraints |
|---|---|---|---|
| `ticker` | `string` | Filter by market ticker | not stated |
| `min_ts` | `integer<int64>` | Filter items after this Unix timestamp | not stated |
| `max_ts` | `integer<int64>` | Filter items before this Unix timestamp | not stated |
| `limit` | `integer<int64>` | Number of results per page | default `100`, max `1000`, range `0 <= x <= 1000` |
| `cursor` | `string` | Pagination cursor; use value from previous response; empty for first page | — |
| `is_block_trade` | `boolean` | Filter trades by whether they are block trades | omit = all; `true` = block only; `false` = non-block only |

## Response fields — CORRECTED 2026-09-13

```json
{
  "trades": [
    {
      "trade_id": "<string>",
      "ticker": "<string>",
      "count_fp": "10.00",
      "yes_price_dollars": "0.5600",
      "no_price_dollars": "0.5600",
      "taker_outcome_side": "yes",
      "taker_book_side": "ask",
      "taker_side": "yes",
      "created_time": "2023-11-07T05:31:56Z",
      "is_block_trade": true
    }
  ],
  "cursor": "<string>"
}
```

- Error object fields: `code`, `message`, `details` (all `string`).
- The 2026-08-14 version of this file listed this object **without** `taker_outcome_side`,
  `taker_book_side` and `taker_side`. That omission is corrected above.

## `created_time`
- Field name: `created_time`, present on every object in the `trades` array.
- Example value: `"2023-11-07T05:31:56Z"` (RFC3339/ISO-8601 UTC in the example).
- The page does NOT explicitly define the semantic meaning of `created_time` or state a format spec beyond the example.

## Do not treat as fact
- That `created_time` equals trade fill time is INFERENCE here; it is only implied by the historical-data page, which partitions trades by `trades_created_ts` = "Trade fill time" (see 04-kalshi-historical-data.md).
- Second-level precision in the example does not prove the API never returns sub-second precision.
- The example's `yes_price_dollars` / `no_price_dollars` values are placeholders and do not sum to 1.

## Lesson recorded
This file asserted a response schema for a month, and a downstream recommendation was built on the
implied absence of a field that was in fact present and required. **An extracted schema is a claim
about a page at a point in time, not about the API.** Where a field's absence is load-bearing,
re-fetch before relying on it. Same failure class as the 441-minute NBM latency and the orphan 0.9%.

SOURCING TAG: VERIFIED (primary, retrieved 2026-08-14; response schema re-verified and corrected
2026-09-13); `created_time` semantics = INFERENCE.
