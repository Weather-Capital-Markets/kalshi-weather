"""Tests for NBM latency check helpers (no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd

from analysis.nbm_latency_check import (
    NbmLatencyChecker,
    evaluate_hard_stop,
    mirror_lag_minutes,
    parse_last_modified_header,
)


def test_parse_last_modified_header_to_utc() -> None:
    parsed = parse_last_modified_header("Wed, 21 Aug 2024 07:12:34 GMT")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.year == 2024


def test_mirror_lag_minutes_arithmetic() -> None:
    nominal = datetime(2024, 8, 20, 0, 0, tzinfo=timezone.utc)
    last_modified = datetime(2024, 8, 20, 1, 30, tzinfo=timezone.utc)
    assert mirror_lag_minutes(nominal, last_modified) == 90.0


def test_evaluate_hard_stop_triggers_at_p90() -> None:
    lags = [30.0, 45.0, 55.0, 70.0, 90.0]
    hard_stop, median, p90, maximum = evaluate_hard_stop(lags, assumed_min=60)
    assert hard_stop is True
    assert p90 == 82.0
    assert maximum == 90.0
    assert median == 55.0


def test_evaluate_hard_stop_passes_when_p90_within_assumption() -> None:
    lags = [30.0, 40.0, 50.0, 55.0, 58.0]
    hard_stop, _median, p90, _maximum = evaluate_hard_stop(lags, assumed_min=60)
    assert hard_stop is False
    assert p90 <= 60.0


def test_checker_run_mocked_head(tmp_path, monkeypatch) -> None:
    config = {
        "nbm_archive": {
            "base_url": "https://example.com",
            "publication_latency_min": 60,
            "snapshot_horizon_h": 24,
        },
        "nbm_latency_check": {
            "assumed_latency_min": 60,
            "min_cycles": 2,
        },
    }
    checker = NbmLatencyChecker(config)

    def fake_panel_a(self, *, end_day=None):
        return pd.DataFrame(
            [
                {
                    "panel": "A",
                    "mirror_lag_min": 45.0,
                    "http_status": 200,
                    "method": "mock",
                },
                {
                    "panel": "A",
                    "mirror_lag_min": 50.0,
                    "http_status": 200,
                    "method": "mock",
                },
            ]
        )

    def fake_panel_b(self, *, end_day=None):
        return pd.DataFrame(
            [
                {
                    "panel": "B",
                    "vintage_safe_at_snapshot": True,
                    "mirror_lag_min": 45.0,
                }
            ]
        )

    monkeypatch.setattr(NbmLatencyChecker, "probe_panel_a", fake_panel_a)
    monkeypatch.setattr(NbmLatencyChecker, "probe_panel_b", fake_panel_b)
    checker.client = MagicMock()
    out_dir = tmp_path / "out"
    assert checker.run(out_dir) == 0
    assert (out_dir / "nbm_latency_check.csv").exists()
    checker.close()


def test_checker_hard_stop_exit_code(tmp_path, monkeypatch) -> None:
    config = {
        "nbm_archive": {"publication_latency_min": 60},
        "nbm_latency_check": {"assumed_latency_min": 60, "min_cycles": 1},
    }
    checker = NbmLatencyChecker(config)

    def fake_panel_a(self, *, end_day=None):
        return pd.DataFrame([{"panel": "A", "mirror_lag_min": 400.0}])

    def fake_panel_b(self, *, end_day=None):
        return pd.DataFrame([{"panel": "B", "vintage_safe_at_snapshot": False}])

    monkeypatch.setattr(NbmLatencyChecker, "probe_panel_a", fake_panel_a)
    monkeypatch.setattr(NbmLatencyChecker, "probe_panel_b", fake_panel_b)
    checker.client = MagicMock()
    assert checker.run(tmp_path / "out") == 2
    checker.close()
