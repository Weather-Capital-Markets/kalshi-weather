"""Pull CLINYC-as-issued highs from IEM AFOS. Labels, not METAR/GHCN-D."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from calendar import monthrange
from datetime import date
from pathlib import Path

from ingestion.cli_labels import month_range, parse_modern_product, split_products

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"


def fetch_month(year: int, month: int) -> str:
    last = monthrange(year, month)[1]
    params = {
        "pil": "CLINYC",
        "fmt": "text",
        "sdate": f"{year:04d}-{month:02d}-01T00:00:00Z",
        "edate": f"{year:04d}-{month:02d}-{last:02d}T23:59:59Z",
        "limit": "9999",
        "order": "asc",
    }
    url = IEM + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "wxmm-v0-clinyc/1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", errors="replace")


def latest_full_day(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Keep the last non-intermediate issuance per climate day."""
    best: dict[str, dict[str, object]] = {}
    for row in rows:
        if row.get("is_same_day_intermediate"):
            continue
        if row.get("high_F") is None:
            continue
        day = str(row["climate_date"])
        prev = best.get(day)
        if prev is None or str(row.get("issuance_ts_utc") or "") >= str(
            prev.get("issuance_ts_utc") or ""
        ):
            best[day] = row
    return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CLINYC as-issued highs")
    parser.add_argument("--start", default="2022-12-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--out", type=Path, default=Path("analysis/out/v0/clinyc.json"))
    args = parser.parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    parsed: list[dict[str, object]] = []
    for month in month_range(start, end):
        year, mon = int(month[:4]), int(month[5:7])
        text = fetch_month(year, mon)
        for product in split_products(text):
            row = parse_modern_product(product, source_month=month)
            if row:
                parsed.append(row)
        print(f"{month} products_kept={len(parsed)}", file=sys.stderr)
    labels = latest_full_day(parsed)
    payload = {
        "source": "clinyc_as_issued",
        "n_issuances_parsed": len(parsed),
        "n_climate_days": len(labels),
        "labels": labels,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"n_climate_days": len(labels), "n_issuances_parsed": len(parsed)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
