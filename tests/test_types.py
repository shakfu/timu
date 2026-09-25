"""Tests for Usage arithmetic and budget checks."""

from __future__ import annotations

import pytest

from timu import Budget, Usage


def test_usage_add() -> None:
    total = Usage(1, 2, 3, 4, 0.5) + Usage(10, 20, 30, 40, 1.0)
    assert total == Usage(11, 22, 33, 44, 1.5)
    assert total.tokens == 77


@pytest.mark.parametrize(
    ("usage", "elapsed", "limit"),
    [
        (Usage(turns=5), 0, "turns"),
        (Usage(tool_calls=5), 0, "tool_calls"),
        (Usage(input_tokens=3, output_tokens=2), 0, "tokens"),
        (Usage(cost_usd=5.0), 0, "cost_usd"),
        (Usage(), 5, "wall_seconds"),
        (Usage(turns=4, tool_calls=4, input_tokens=4, cost_usd=4.9), 4.9, None),
    ],
)
def test_exceeds(usage: Usage, elapsed: float, limit: str | None) -> None:
    budget = Budget(turns=5, tool_calls=5, tokens=5, cost_usd=5.0, wall_seconds=5)
    assert usage.exceeds(budget, elapsed) == limit


def test_no_cost_limit() -> None:
    assert Usage(cost_usd=1e9).exceeds(Budget(), 0) is None
