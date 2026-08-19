"""KNYC/KLGA ASOS/METAR backfill from IEM.

Allowed DB: storage.backfill_db only. Never open storage.heartbeat_db.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ingestion.client import KalshiClient
from ingestion.config_loader import load_config
from ingestion.heartbeat import connect, init_schema, record_attempt
from ingestion.state import (
    asos_month_complete,
    init_backfill_schema,
    init_state_schema,
    set_asos_month_complete,
)
from ingestion.writer import RawJsonlWriter, utc_now_iso

logger = logging.getLogger(__name__)


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


def resolve_stations(asos_cfg: dict[str, Any]) -> list[str]:
    stations = asos_cfg.get("stations")
    if isinstance(stations, list) and stations:
        return [str(station) for station in stations]
    legacy = asos_cfg.get("station")
    if isinstance(legacy, str) and legacy.strip():
        return [legacy.strip()]
    return ["NYC"]


class AsosObsBackfill:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        storage = config["storage"]
        self.writer = RawJsonlWriter(storage["raw_dir"])
        self.conn = connect(storage["backfill_db"])
        init_schema(self.conn)
        init_state_schema(self.conn)
        init_backfill_schema(self.conn)
        asos = config.get("asos_obs") or {}
        self.start = date.fromisoformat(str(asos.get("start_date") or "2025-05-01"))
        self.stations = resolve_stations(asos)
        self.network = str(asos.get("network") or "NY_ASOS")
        self.data_fields = list(asos.get("data_fields") or ["tmpf"])
        self.iem_url = str(
            asos.get("iem_asos_url")
            or "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        )
        parsed = urlparse(self.iem_url)
        self.iem_path = parsed.path or "/cgi-bin/request/asos.py"
        iem_base = (
            f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else "https://mesonet.agron.iastate.edu/"
        )
        iem_config = {
            "api": {
                **config["api"],
                "base_url": iem_base,
                "timeout_sec": 60,
                "max_requests_per_sec": 1,
                "paths": config.get("api", {}).get("paths") or {"markets": "/markets"},
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
        months = month_range(self.start, today)
        for station in self.stations:
            if self._shutdown:
                break
            for month in months:
                if self._shutdown:
                    break
                if asos_month_complete(self.conn, station, month):
                    continue
                self._fetch_month(station, month, can_complete=month != current_month)
        return 0

    def _fetch_month(self, station: str, month: str, *, can_complete: bool = True) -> None:
        if asos_month_complete(self.conn, station, month):
            return
        year, mon = (int(part) for part in month.split("-"))
        last = monthrange(year, mon)[1]
        params = {
            "network": self.network,
            "station": station,
            "data": ",".join(self.data_fields),
            # IEM asos.py requires timezone-aware ISO timestamps (HTTP 422 otherwise).
            "sts": f"{year:04d}-{mon:02d}-01T00:00:00Z",
            "ets": f"{year:04d}-{mon:02d}-{last:02d}T23:59:59Z",
            "tz": "UTC",
            "format": "onlycomma",
            "latlon": "no",
            "elev": "no",
            "missing": "M",
            "trace": "T",
            "direct": "no",
            "report_type": "3",
        }
        result = self.client.get(self.iem_path, params=params)
        record_attempt(
            self.conn,
            ts_utc=utc_now_iso(),
            endpoint=self.iem_path,
            ticker=f"{station}:{month}",
            ok=result.ok,
            http_status=result.status_code,
            latency_ms=result.latency_ms,
            error_text=result.error_text,
        )
        if not result.ok:
            logger.warning(
                "IEM ASOS month %s station=%s failed status=%s",
                month,
                station,
                result.status_code,
            )
            return
        text = result.text_body or ""
        if not text.strip():
            logger.warning(
                "IEM ASOS month %s station=%s returned empty body; not marking complete",
                month,
                station,
            )
            return
        payload = {
            "text": text,
            "month": month,
            "station": station,
            "network": self.network,
            "http_status": result.status_code,
        }
        self.writer.write(
            ts_utc=utc_now_iso(),
            endpoint=self.iem_path,
            category="asos_obs",
            key=f"{station}_{month}",
            http_status=result.status_code,
            latency_ms=result.latency_ms,
            payload=payload,
        )
        if can_complete:
            set_asos_month_complete(self.conn, station, month, utc_now_iso())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill NYC-area ASOS observations from IEM")
    parser.add_argument("--config", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    backfill = AsosObsBackfill(load_config(args.config))
    backfill.install_signal_handlers()
    try:
        return backfill.run()
    finally:
        backfill.close()


if __name__ == "__main__":
    raise SystemExit(main())
