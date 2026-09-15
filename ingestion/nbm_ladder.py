"""NBM percentile ladder post-processing: dedupe and isotonic repair.

Non-monotone quantile ladders can produce negative bracket probabilities in K2.
We apply pool-adjacent-violators (PAV) isotonic regression for increasing
temperature with percentile level.
"""

from __future__ import annotations

from typing import Any


def dedupe_ladder_by_percentile(
    ladder: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Keep the first entry per percentile_level (lowest byte_offset wins)."""
    by_level: dict[int, dict[str, Any]] = {}
    for entry in sorted(ladder, key=lambda item: (item["percentile_level"], item["byte_offset"])):
        level = int(entry["percentile_level"])
        if level not in by_level:
            by_level[level] = entry
    dropped = len(ladder) - len(by_level)
    ordered = [by_level[level] for level in sorted(by_level)]
    return ordered, dropped


def isotonic_maximize(levels: list[int], values: list[float]) -> list[float]:
    """PAV isotonic regression: non-decreasing values in level order."""
    if not levels:
        return []
    if len(levels) != len(values):
        raise ValueError("levels and values must have the same length")
    n = len(values)
    adjusted = list(values)
    weights = [1.0] * n
    blocks: list[tuple[int, int]] = [(index, index) for index in range(n)]

    index = 0
    while index < len(blocks) - 1:
        start_a, end_a = blocks[index]
        start_b, end_b = blocks[index + 1]
        sum_a = sum(adjusted[start_a : end_a + 1])
        sum_b = sum(adjusted[start_b : end_b + 1])
        weight_a = sum(weights[start_a : end_a + 1])
        weight_b = sum(weights[start_b : end_b + 1])
        mean_a = sum_a / weight_a
        mean_b = sum_b / weight_b
        if mean_a <= mean_b:
            index += 1
            continue
        new_start = start_a
        new_end = end_b
        new_sum = sum_a + sum_b
        new_weight = weight_a + weight_b
        new_mean = new_sum / new_weight
        for pos in range(new_start, new_end + 1):
            adjusted[pos] = new_mean
        blocks[index] = (new_start, new_end)
        del blocks[index + 1]
        if index > 0:
            index -= 1

    return adjusted


def apply_isotonic_to_ladder(
    ladder: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Return ladder with value_f isotonic-adjusted; preserve value_f_raw."""
    if not ladder:
        return [], False
    ordered = sorted(ladder, key=lambda item: int(item["percentile_level"]))
    levels = [int(item["percentile_level"]) for item in ordered]
    raw_values = [float(item["value_f"]) for item in ordered]
    adjusted = isotonic_maximize(levels, raw_values)
    changed = any(abs(raw - adj) > 1e-9 for raw, adj in zip(raw_values, adjusted, strict=True))
    repaired: list[dict[str, Any]] = []
    for entry, raw, adj in zip(ordered, raw_values, adjusted, strict=True):
        item = dict(entry)
        item["value_f_raw"] = raw
        item["value_f"] = adj
        item["isotonic_adjusted"] = abs(raw - adj) > 1e-9
        repaired.append(item)
    return repaired, changed
