"""What every built-in provider shares: one retry rule, and how to read a failure.

A failure is retried with backoff, or after Retry-After, at most `retries` times.
A retry after text reached on_text marks where the reply restarts, so the console
and trace show both attempts, while only the retry's text enters the conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from timu.provider.base import OnText, Reply

MAX_RETRY_AFTER = 60.0  # seconds; a longer Retry-After falls back to backoff
CONTEXT_HINTS = (
    "context size",
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "too many tokens",
)


@dataclass(frozen=True)
class Failure:
    error: str
    retryable: bool
    delay: float | None = None  # from Retry-After
    delivered: bool = False  # text reached on_text before the failure


def retrying(
    attempt: Callable[[], Reply | Failure],
    on_text: OnText | None,
    retries: int,
    retry_delay: float,
    sleep: Callable[[float], None],
) -> Reply:
    """attempt until it returns a Reply or a failure that is final; a final failure
    becomes a Reply with stop="error"."""
    for n in range(retries + 1):
        outcome = attempt()
        if isinstance(outcome, Reply):
            return outcome
        if not outcome.retryable or n == retries:
            return Reply("", stop="error", error=outcome.error)
        if outcome.delivered and on_text:
            on_text(f"\n[{outcome.error}; retrying the reply]\n")
        sleep(outcome.delay if outcome.delay is not None else retry_delay * 2**n)
    raise AssertionError("unreachable")  # the loop always returns


def is_context_error(body: str) -> bool:
    """OpenAI's code and llama-server's type first; wording, which varies, after."""
    if "context_length_exceeded" in body or "exceed_context_size_error" in body:
        return True
    lower = body.lower()
    return any(h in lower for h in CONTEXT_HINTS)


def retry_after(headers: Any) -> float | None:
    """Retry-After in seconds, if given as a number and not too long."""
    try:
        secs = float(headers.get("retry-after", ""))
    except (TypeError, ValueError, AttributeError):
        return None
    return secs if 0 <= secs <= MAX_RETRY_AFTER else None


def retryable_status(code: int) -> bool:
    """A status worth another try: timeouts, conflicts, rate limits, server errors."""
    return code in (408, 409, 429) or code >= 500
