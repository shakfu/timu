"""Checks for a provider adapter's tests: the parts of the contract in base that the
types do not enforce. A package runs them on the replies its adapter builds from
recorded API responses.

    from timu.provider.testing import reply_problems

    def test_a_cached_reply():
        reply = adapter_from(recorded_response).complete(messages, tools)
        assert reply_problems(reply) == []
"""

from __future__ import annotations

import json
import math

from timu.provider.base import Reply, usage_problem


def reply_problems(reply: Reply) -> list[str]:
    """Each way reply breaks a rule stated in base; empty if none."""
    problems = []
    u = reply.usage
    counts = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cached_input_tokens": u.cached_input_tokens,
        "cache_write_input_tokens": u.cache_write_input_tokens,
    }
    problems += [
        f"usage.{name} is {n!r}; it must be an int >= 0"
        for name, n in counts.items()
        if not isinstance(n, int) or isinstance(n, bool) or n < 0
    ]
    if not problems and (problem := usage_problem(u)):
        problems.append(f"usage: {problem}")
    if (u.turns, u.tool_calls) != (0, 0):
        problems.append("usage.turns and usage.tool_calls are the agent's; leave 0")
    if not (math.isfinite(u.cost_usd) and u.cost_usd >= 0):
        problems.append(f"usage.cost_usd is {u.cost_usd!r}; it must be finite, >= 0")
    if not reply.cost_known and u.cost_usd:
        problems.append("cost_known is False but usage.cost_usd is set")
    if reply.stop == "error" and not reply.error:
        problems.append('stop is "error" but error is empty')
    for c in reply.tool_calls:
        parsed = _json(c.raw)
        if c.arguments is None and isinstance(parsed, dict):
            problems.append(f"tool call {c.id!r}: arguments is None but raw is JSON")
        if c.arguments is not None and c.raw and parsed != dict(c.arguments):
            problems.append(f"tool call {c.id!r}: arguments do not match raw")
    return problems


def _json(raw: str) -> object:
    try:
        return json.loads(raw)
    except ValueError:
        return None
