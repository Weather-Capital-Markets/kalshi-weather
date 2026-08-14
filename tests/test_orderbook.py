"""Tests for orderbook top-of-book extraction."""

from __future__ import annotations

import pytest

from analysis.orderbook import top_of_book_from_payload


def test_top_of_book_from_fixture() -> None:
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.4200", "100.00"], ["0.4100", "50.00"]],
            "no_dollars": [["0.5800", "75.00"]],
        }
    }
    bid, ask = top_of_book_from_payload(payload)
    assert bid == 0.42
    assert ask == pytest.approx(0.42)
