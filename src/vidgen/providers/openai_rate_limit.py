"""Shared 429 handling for the raw-httpx OpenAI adapters.

OpenAI answers ``429`` for two situations that deserve opposite responses. A
rate limit is temporary: the call is worth repeating once the window clears,
and the response says how long that takes in ``Retry-After``. A spend limit —
an exhausted quota or a hard billing cap — is terminal: every retry buys
another rejection, so it surfaces as a budget denial, which the pipelines
already stop on.

An adapter that would otherwise call ``response.raise_for_status()`` straight
after its POST wraps the call in :func:`send_with_backoff`. That waits out the
rate limit instead of handing the exception to a pipeline retry loop that would
re-issue the request immediately, and only raises :class:`OpenAIRateLimited`
once its attempts are spent. The wait is jittered upwards so the scenes a
single ``asyncio.gather`` fired together — and therefore rate limited together
— do not all wake in the same instant and rebuild the herd.

Adapters that stream their response (narration audio) cannot be retried by
re-running a context manager from here; they read the body themselves and ask
:func:`retry_delay_for_rate_limit` for the same decision.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

import httpx

from vidgen.db.cost_repository import BudgetExceededError

RATE_LIMIT_STATUS: Final = 429

#: ``error.code``/``error.type`` values that mean the account cannot pay for
#: the call. Anything else behind a 429 is treated as a passing rate limit.
SPEND_LIMIT_CODES: Final[frozenset[str]] = frozenset(
    {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}
)

#: Message fragments OpenAI uses for the same conditions when the structured
#: code is missing. Matched case-insensitively against ``error.message``.
SPEND_LIMIT_MESSAGES: Final[tuple[str, ...]] = (
    "exceeded your current quota",
    "billing hard limit",
    "spend limit",
)


class OpenAIRateLimited(RuntimeError):
    """A 429 that was still a rate limit after the adapter finished backing off."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OpenAISpendLimitExceeded(BudgetExceededError):
    """A 429 that reports an exhausted quota or a hard billing cap.

    It subclasses :class:`BudgetExceededError` so the stages that already treat
    a project budget denial as terminal treat a provider spend limit the same
    way, instead of spending their remaining attempts on a call that cannot
    succeed until somebody pays the bill.
    """


@dataclass(frozen=True, slots=True)
class RateLimitBackoff:
    """How long an adapter waits out a rate limit before giving up.

    ``max_attempts`` counts the whole attempts, so the default sends the
    request at most four times and sleeps at most three times.
    """

    max_attempts: int = 4
    base_seconds: float = 1.0
    max_seconds: float = 120.0
    #: Upward-only jitter, as a fraction of the delay. It never shortens a
    #: ``Retry-After`` the provider asked for, it only spreads a batch of
    #: simultaneously rate-limited callers out across the window.
    jitter_ratio: float = 0.25


DEFAULT_BACKOFF: Final = RateLimitBackoff()


def parse_retry_after(
    headers: httpx.Headers | Mapping[str, str], *, now: datetime | None = None
) -> float | None:
    """Return the ``Retry-After`` delay in seconds, or ``None`` when absent.

    Both RFC 9110 forms are accepted: a delay in seconds, and an HTTP date,
    which is converted to a delay against ``now``. A past date, or a header
    that parses as neither, yields ``0.0`` and ``None`` respectively.
    """
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0.0, (deadline - (now or datetime.now(UTC))).total_seconds())


def spend_limit_reason(response: httpx.Response) -> str | None:
    """Return the provider code behind a terminal 429, or ``None`` if temporary.

    The response body must already be read; a streamed response needs an
    ``await response.aread()`` first.
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str) and value in SPEND_LIMIT_CODES:
            return value
    message = error.get("message")
    if isinstance(message, str):
        lowered = message.lower()
        if any(marker in lowered for marker in SPEND_LIMIT_MESSAGES):
            return "spend_limit"
    return None


def retry_delay_for_rate_limit(
    response: httpx.Response,
    *,
    attempt: int,
    policy: RateLimitBackoff = DEFAULT_BACKOFF,
) -> float:
    """Seconds to wait before repeating a rate-limited request.

    ``attempt`` is the 1-based number of the attempt that was just rejected.
    Raises :class:`OpenAISpendLimitExceeded` when the body reports a terminal
    spend limit, and :class:`OpenAIRateLimited` once the policy's attempts are
    spent. The response body must already be read.
    """
    reason = spend_limit_reason(response)
    if reason is not None:
        # Never echo the provider body: it can carry the prompt back.
        raise OpenAISpendLimitExceeded(f"openai_spend_limit_exceeded: {reason}")
    retry_after = parse_retry_after(response.headers)
    if attempt >= policy.max_attempts:
        raise OpenAIRateLimited(
            f"openai_rate_limited: still rate limited after {attempt} attempts",
            retry_after=retry_after,
        )
    base = retry_after if retry_after is not None else policy.base_seconds * 2 ** (attempt - 1)
    return min(base, policy.max_seconds) * (1 + random.random() * policy.jitter_ratio)


async def send_with_backoff(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    policy: RateLimitBackoff = DEFAULT_BACKOFF,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> httpx.Response:
    """Run ``send`` until it answers anything but 429, waiting in between.

    ``send`` is re-invoked per attempt rather than the response being replayed,
    so a caller that has to rebuild its request — reopening an upload stream,
    say — can do that inside the callable. The returned response is handed back
    untouched: callers still decide what to do with other statuses. ``sleep``
    defaults to :func:`asyncio.sleep`, resolved per call so a test can wait out
    a rate limit without spending the wall clock on it.
    """
    attempt = 1
    while True:
        response = await send()
        if response.status_code != RATE_LIMIT_STATUS:
            return response
        await response.aread()
        delay = retry_delay_for_rate_limit(response, attempt=attempt, policy=policy)
        await (sleep or asyncio.sleep)(delay)
        attempt += 1


__all__ = [
    "DEFAULT_BACKOFF",
    "RATE_LIMIT_STATUS",
    "SPEND_LIMIT_CODES",
    "SPEND_LIMIT_MESSAGES",
    "OpenAIRateLimited",
    "OpenAISpendLimitExceeded",
    "RateLimitBackoff",
    "parse_retry_after",
    "retry_delay_for_rate_limit",
    "send_with_backoff",
    "spend_limit_reason",
]
