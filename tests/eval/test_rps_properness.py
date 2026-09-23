"""The primary score is the ranked probability score.

A perfect forecast scores 0, distance scores worse than proximity, and the
expected score is minimised by reporting the true distribution. ``order`` is
required so ticker-string order cannot be reached by omission.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from wxmm.eval.scores import ranked_probability_score

ORDER = ("a", "b", "c")


def _one_hot(realised: str) -> dict[str, Decimal]:
    return {key: Decimal(1) if key == realised else Decimal(0) for key in ORDER}


@pytest.mark.parametrize("realised", ["a", "b", "c"])
def test_perfect_forecast_scores_zero(realised: str) -> None:
    score = ranked_probability_score(_one_hot(realised), realised, order=ORDER)
    assert score == Decimal(0)


def test_far_bracket_scores_worse_than_adjacent() -> None:
    adjacent = {"a": Decimal(0), "b": Decimal(1), "c": Decimal(0)}
    far = {"a": Decimal(0), "b": Decimal(0), "c": Decimal(1)}
    near = ranked_probability_score(adjacent, "a", order=ORDER)
    distant = ranked_probability_score(far, "a", order=ORDER)
    assert near < distant


def test_expected_score_is_minimised_at_the_true_law() -> None:
    truth = {"a": Decimal("0.2"), "b": Decimal("0.5"), "c": Decimal("0.3")}
    swapped = {"a": Decimal("0.2"), "b": Decimal("0.3"), "c": Decimal("0.5")}

    def expected(forecast: dict[str, Decimal]) -> Decimal:
        return sum(
            (
                truth[outcome] * ranked_probability_score(forecast, outcome, order=ORDER)
                for outcome in ORDER
            ),
            Decimal(0),
        )

    assert expected(truth) < expected(swapped)


def test_order_is_a_required_keyword() -> None:
    with pytest.raises(TypeError):
        ranked_probability_score(_one_hot("a"), "a")  # type: ignore[call-arg]


@pytest.mark.parametrize("order", [("a", "b"), ("a", "b", "c", "a")])
def test_order_must_list_exactly_the_forecast_brackets(order: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="exactly"):
        ranked_probability_score(_one_hot("a"), "a", order=order)
