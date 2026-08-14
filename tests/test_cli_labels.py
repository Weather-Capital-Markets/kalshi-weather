"""CLINYC parser and product-splitter tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ingestion.cli_labels import CliLabelBackfill, parse_modern_product, split_products
from ingestion.client import RequestResult
from ingestion.state import month_complete, set_month_complete
from ingestion.writer import utc_now_iso

FIXTURE = Path(__file__).parent / "fixtures" / "clinyc_cdus41.txt"


def test_splitter_handles_concatenated_soh_etx_products() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    assert "\x01" in text and "\x03" in text
    products = split_products(text)
    assert len(products) >= 2
    assert all("CDUS41" in product for product in products)


def test_splitter_falls_back_to_wmo_header_when_no_control_chars() -> None:
    text = FIXTURE.read_text(encoding="utf-8").replace("\x01", "").replace("\x03", "")
    products = split_products(text)
    assert len(products) >= 2
    assert all(product.startswith("CDUS41") for product in products)


def test_parser_final_and_intermediate_issuances() -> None:
    products = split_products(FIXTURE.read_text(encoding="utf-8"))
    parsed = [parse_modern_product(p, source_month="2026-07") for p in products]
    parsed = [row for row in parsed if row]
    assert len(parsed) == 2
    finals = [row for row in parsed if not row["is_same_day_intermediate"]]
    intermediates = [row for row in parsed if row["is_same_day_intermediate"]]
    assert len(finals) == 1
    assert len(intermediates) == 1
    final = finals[0]
    assert final["climate_date"] == "2026-07-04"
    assert final["high_F"] == 94
    assert final["time_of_high_raw"] == "455 PM"
    assert final["time_col_label"] == "(LST)"
    assert final["issuance_ts_utc"] == "2026-07-05T06:20:00Z"
    assert intermediates[0]["time_of_high_raw"] == "105 PM"


def test_month_resume_skips_completed(tmp_path: Path) -> None:
    config = {
        "api": {
            "base_url": "https://example.test/",
            "paths": {"markets": "/markets"},
            "timeout_sec": 5,
            "max_requests_per_sec": 100,
            "max_retries": 0,
        },
        "storage": {
            "raw_dir": str(tmp_path / "raw"),
            "backfill_db": str(tmp_path / "backfill.sqlite"),
            "labels_csv": str(tmp_path / "clinyc.csv"),
        },
        "cli_labels": {"start_date": "2026-07-01"},
        "logging": {"level": "WARNING"},
    }
    app = CliLabelBackfill(config)

    class BoomClient:
        def close(self) -> None:
            return None

        def get(self, path: str, *, params=None) -> RequestResult:
            raise AssertionError("completed months must not be refetched")

    try:
        set_month_complete(app.conn, "2026-07", utc_now_iso())
        app.client = BoomClient()  # type: ignore[assignment]
        with patch("ingestion.cli_labels.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 7, 15, tzinfo=timezone.utc)
            assert app.run() == 0
        assert month_complete(app.conn, "2026-07")
    finally:
        app.close()
