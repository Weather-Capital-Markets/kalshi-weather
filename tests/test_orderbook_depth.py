"""Tests for multi-level orderbook depth metrics."""

from __future__ import annotations

import pytest

from analysis.orderbook import (
    depth_metrics_from_payload,
    depth_within_cents,
    dollar_depth_within_cents,
    parse_yes_ask_levels,
    parse_yes_bid_levels,
    size_at_best_levels,
)


def _sample_payload() -> dict:
    return {
        "orderbook_fp": {
            "yes_dollars": [
                ["0.5000", "100.00"],
                ["0.4900", "50.00"],
                ["0.4800", "25.00"],
            ],
            "no_dollars": [
                ["0.5200", "80.00"],
                ["0.5300", "40.00"],
            ],
        }
    }


def test_size_at_best_levels_sums_top_n() -> None:
    bids = parse_yes_bid_levels(_sample_payload()["orderbook_fp"])
    assert size_at_best_levels(bids, 1) == 100.0
    assert size_at_best_levels(bids, 2) == 150.0
    assert size_at_best_levels(bids, 5) == 175.0


def test_depth_within_two_cents() -> None:
    book = _sample_payload()["orderbook_fp"]
    bids = parse_yes_bid_levels(book)
    asks = parse_yes_ask_levels(book)
    mid = 0.49
    depth = depth_within_cents(mid, bids, asks, 2)
    assert depth == pytest.approx(295.0)


def test_dollar_depth_within_two_cents() -> None:
    book = _sample_payload()["orderbook_fp"]
    bids = parse_yes_bid_levels(book)
    asks = parse_yes_ask_levels(book)
    mid = 0.49
    dollars = dollar_depth_within_cents(mid, bids, asks, 2)
    expected = 0.50 * 100 + 0.49 * 50 + 0.48 * 25 + 0.48 * 80 + 0.47 * 40
    assert dollars == pytest.approx(expected)


def test_depth_metrics_from_payload_keys() -> None:
    metrics = depth_metrics_from_payload(_sample_payload())
    assert metrics is not None
    assert metrics["yes_size_5"] == 175.0
    assert metrics["depth_within_2c"] > 0
    assert metrics["dollar_depth_within_2c"] > 0
    assert metrics["imbalance_top"] is not None
