"""Tests for NBM window mismatch measurement."""

from __future__ import annotations

import pandas as pd

from analysis.window_mismatch import _mismatch_rows, summarize_by_season


def test_mismatch_rows_under_lst_and_ldt() -> None:
    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "time_of_high_raw": "300 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            }
        ]
    )
    lst = _mismatch_rows(labels, "lst")
    ldt = _mismatch_rows(labels, "ldt")
    assert len(lst) == 1
    assert len(ldt) == 1
    summary = summarize_by_season(pd.concat([lst, ldt], ignore_index=True))
    assert set(summary["convention"]) == {"lst", "ldt"}


def test_window_mismatch_refuses_headline_when_unknown(monkeypatch) -> None:
    from analysis import window_mismatch as module

    labels = pd.DataFrame(
        [
            {
                "climate_date": "2026-07-04",
                "time_of_high_raw": "300 PM",
                "is_same_day_intermediate": False,
                "issuance_ts_utc": "2026-07-05T10:00:00Z",
            }
        ]
    )

    def fake_select(_path):
        return labels

    monkeypatch.setattr(module, "select_full_day_labels", fake_select)
    captured: list[str] = []

    def capture_print(*args, **kwargs):
        captured.append(" ".join(str(a) for a in args))

    monkeypatch.setattr("builtins.print", capture_print)
    module.run(
        {"storage": {}, "climate": {"cli_time_convention": "unknown"}},
        module.Path("/tmp/out"),
    )
    assert any("REFUSED" in line for line in captured)
