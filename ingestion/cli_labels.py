"""CLINYC label backfill from IEM AFOS.

Allowed DB: storage.backfill_db only. Never open storage.heartbeat_db.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import signal
import sys
import time
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ingestion.client import KalshiClient
from ingestion.climate_day import MONTHS, parse_nws_issuance_ts
from ingestion.config_loader import load_config
from ingestion.heartbeat import connect, init_schema, record_attempt
from ingestion.state import (
    init_backfill_schema,
    init_state_schema,
    month_complete,
    set_month_complete,
)
from ingestion.validate_units import (
    assert_non_empty_rows,
    validate_temperature_f,
)
from ingestion.writer import RawJsonlWriter, read_jsonl_gz, utc_now_iso

logger = logging.getLogger(__name__)

WMO_HEADER = re.compile(r"(?m)^CDUS41\s+KOKX\b")
SUMMARY_FOR = re.compile(
    r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})",
    re.IGNORECASE,
)
ISSUANCE_LINE = re.compile(
    r"(\d{3,4}\s+[AP]M\s+[A-Z]{3}\s+[A-Z]{3}\s+[A-Z]{3}\s+\d{1,2}\s+\d{4})",
    re.IGNORECASE,
)
TIME_COL_LABEL = re.compile(r"VALUE\s+(\([^)]+\))", re.IGNORECASE)
MAXIMUM_LINE = re.compile(
    r"^\s*MAXIMUM\s+(-?\d+|MM)\s+(\d{1,2}:?\d{0,2}\s*[AP]M)?",
    re.IGNORECASE | re.MULTILINE,
)
# IEM rejects anything larger with HTTP 422. A CLINYC month is ~100 issuances,
# so this is not a binding constraint, but _fetch_month checks anyway.
IEM_MAX_LIMIT = 9999
CSV_FIELDS = [
    "issuance_ts_utc",
    "climate_date",
    "high_F",
    "time_of_high_raw",
    "time_col_label",
    "is_same_day_intermediate",
    "raw_pil",
    "source_month",
]


def month_range(start: date, end: date) -> list[str]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return months


def split_products(text: str) -> list[str]:
    """Split concatenated IEM text into issuances.

    Prefer NOAAPort SOH/ETX framing when present; otherwise split on the
    CDUS41 KOKX WMO header. Framing from retrieve.py is treated as unknown.
    """
    stripped = text.replace("\r\n", "\n")
    if "\x01" in stripped or "\x03" in stripped:
        parts = re.split(r"[\x01\x03]+", stripped)
        return [part.strip() for part in parts if part.strip() and "CLINYC" in part]
    matches = list(WMO_HEADER.finditer(stripped))
    if not matches:
        return [stripped.strip()] if stripped.strip() else []
    chunks: list[str] = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(stripped)
        chunk = stripped[match.start() : end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def _is_modern(product: str) -> bool:
    head = product[:400].upper()
    return "CDUS41" in head and "KOKX" in head and "CLIMATE REPORT" in head


def parse_modern_product(product: str, *, source_month: str) -> dict[str, Any] | None:
    if not _is_modern(product):
        return None
    summary = SUMMARY_FOR.search(product)
    if not summary:
        return None
    month = MONTHS.get(summary.group(1)[:3].upper())
    if month is None:
        return None
    climate_date = date(int(summary.group(3)), month, int(summary.group(2))).isoformat()
    issuance_match = ISSUANCE_LINE.search(product)
    issuance_ts = parse_nws_issuance_ts(issuance_match.group(1)) if issuance_match else None
    label_match = TIME_COL_LABEL.search(product)
    time_col_label = label_match.group(1) if label_match else ""
    max_match = MAXIMUM_LINE.search(product)
    high_raw = max_match.group(1) if max_match else ""
    high_f: int | None
    if not high_raw or high_raw.upper() == "MM":
        high_f = None
    else:
        high_f = int(validate_temperature_f(float(high_raw), label="high_F"))
    time_raw = (max_match.group(2) or "").strip() if max_match else ""
    intermediate = bool(re.search(r"VALID TODAY AS OF", product, re.IGNORECASE))
    if re.search(r"TEMPERATURE \(F\).*?\n\s*TODAY\b", product, re.IGNORECASE | re.DOTALL):
        intermediate = True
    return {
        "issuance_ts_utc": issuance_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if issuance_ts else "",
        "climate_date": climate_date,
        "high_F": high_f,
        "time_of_high_raw": time_raw,
        "time_col_label": time_col_label,
        "is_same_day_intermediate": intermediate,
        "raw_pil": "CLINYC",
        "source_month": source_month,
    }


class CliLabelBackfill:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.conn = connect(storage["backfill_db"])
        init_schema(self.conn)
        init_state_schema(self.conn)
        init_backfill_schema(self.conn)
        self.raw_dir = Path(storage["raw_dir"])
        self.labels_csv = Path(storage.get("labels_csv") or "data/labels/clinyc.csv")
        cli = config.get("cli_labels", {})
        self.start = date.fromisoformat(str(cli.get("start_date") or "2020-01-01"))
        self.iem_url = str(
            cli.get("iem_retrieve_url")
            or "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
        )
        iem_parsed = urlparse(self.iem_url)
        self.iem_path = iem_parsed.path or "/cgi-bin/afos/retrieve.py"
        iem_base = (
            f"{iem_parsed.scheme}://{iem_parsed.netloc}/"
            if iem_parsed.netloc
            else "https://mesonet.agron.iastate.edu/"
        )
        # Dedicated client: IEM is not the Kalshi base URL.
        iem_config = {
            "api": {
                **config["api"],
                "base_url": iem_base,
                "timeout_sec": 60,
                "max_requests_per_sec": 1,
            }
        }
        self.client = KalshiClient(iem_config)
        self._shutdown = False

    def close(self) -> None:
        self.writer.close()
        self.client.close()
        self.conn.close()

    def install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: Any) -> None:
            logger.info("received signal %s, shutting down", signum)
            self._shutdown = True

        signal.signal(signal.SIGTERM, _handle)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, _handle)

    def run(self) -> int:
        today = datetime.now(timezone.utc).date()
        current_month = f"{today.year:04d}-{today.month:02d}"
        for month in month_range(self.start, today):
            if self._shutdown:
                break
            if month_complete(self.conn, month):
                continue
            # The current month is still accumulating issuances, so a successful
            # fetch is not a complete one; marking it done would make every later
            # run skip the rest of the month.
            self._fetch_month(month, can_complete=month != current_month)
        self.rebuild_csv()
        return 0

    def _fetch_month(self, month: str, *, can_complete: bool = True) -> None:
        year, mon = (int(part) for part in month.split("-"))
        last = monthrange(year, mon)[1]
        # IEM retrieve accepts `YYYY-MM-DD HH:MM` (space, no Z). ISO-Z was rejected.
        sdate = f"{year:04d}-{mon:02d}-01 00:00"
        edate = f"{year:04d}-{mon:02d}-{last:02d} 23:59"
        params = {
            "pil": "CLINYC",
            "fmt": "text",
            "sdate": sdate,
            "edate": edate,
            "limit": IEM_MAX_LIMIT,
            "order": "asc",
        }
        result = self.client.get(self.iem_path, params=params)
        record_attempt(
            self.conn,
            ts_utc=utc_now_iso(),
            endpoint=self.iem_path,
            ticker=month,
            ok=result.ok,
            http_status=result.status_code,
            latency_ms=result.latency_ms,
            error_text=result.error_text,
        )
        if not result.ok:
            logger.warning("IEM month %s failed status=%s", month, result.status_code)
            return
        text = result.text_body or ""
        if not text.strip():
            logger.warning(
                "IEM month %s returned empty body; not marking complete",
                month,
            )
            return
        n_products = len(split_products(text))
        if n_products <= 0:
            logger.warning(
                "IEM month %s returned no parseable products; not marking complete",
                month,
            )
            return
        if n_products >= IEM_MAX_LIMIT:
            logger.warning(
                "IEM month %s returned %s products, at the %s cap; the month may be truncated",
                month,
                n_products,
                IEM_MAX_LIMIT,
            )
        payload = {"text": text, "month": month, "http_status": result.status_code}
        self.writer.write(
            ts_utc=utc_now_iso(),
            endpoint=self.iem_path,
            category="cli_labels",
            key=month,
            http_status=result.status_code,
            latency_ms=result.latency_ms,
            payload=payload,
        )
        if can_complete:
            set_month_complete(self.conn, month, utc_now_iso())

    def rebuild_csv(self) -> None:
        rows: list[dict[str, Any]] = []
        skipped = 0
        raw_paths = sorted(self.raw_dir.glob("*/cli_labels/*.jsonl.gz"))
        if not raw_paths:
            logger.info("no cli_labels raw captures; skipping clinyc.csv rebuild")
            return
        for path in raw_paths:
            for record in read_jsonl_gz(path):
                payload = record.get("payload") or {}
                text = payload.get("text") if isinstance(payload, dict) else ""
                month = payload.get("month") if isinstance(payload, dict) else path.stem
                if not isinstance(text, str):
                    continue
                for product in split_products(text):
                    parsed = parse_modern_product(product, source_month=str(month))
                    if parsed is None:
                        skipped += 1
                        continue
                    rows.append(parsed)
        assert_non_empty_rows(len(rows), what="clinyc.csv label rows")
        self.labels_csv.parent.mkdir(parents=True, exist_ok=True)
        with self.labels_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        logger.info("wrote %s rows=%s skipped_non_modern=%s", self.labels_csv, len(rows), skipped)


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="ts_utc=%(asctime)sZ level=%(levelname)s logger=%(name)s message=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[handler], force=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CLINYC IEM backfill")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--rebuild-csv-only",
        action="store_true",
        help="Rebuild clinyc.csv from already-captured raw pages",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    configure_logging(config.get("logging", {}).get("level", "INFO"))
    app = CliLabelBackfill(config)
    app.install_signal_handlers()
    try:
        if args.rebuild_csv_only:
            app.rebuild_csv()
            return 0
        return app.run()
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
