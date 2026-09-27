#!/usr/bin/env python3
"""Retry / rate-limit helpers for analytics fetchers."""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

# Wait 1s, then 3s, then 9s, then 27s between rate-limit retries.
DEFAULT_BACKOFF_SECONDS = (1, 3, 9, 27)


def is_rate_limit_error(exc: BaseException | str | None) -> bool:
    text = str(exc or "").lower()
    type_name = ""
    if isinstance(exc, BaseException):
        type_name = type(exc).__name__.lower()
    blob = f"{type_name} {text}"
    markers = (
        "429",
        "too many requests",
        "rate limit",
        "ratelimit",
        "throttled",
        "clientthrottled",
        "please wait a few minutes",
        "pleasewaitfewminutes",
        "quota exceeded",
        "user has exceeded",
        "resource exhausted",
    )
    return any(m in blob for m in markers)


def with_rate_limit_retry(
    platform: str,
    fn: Callable[[], T],
    *,
    backoff_seconds: tuple[int, ...] = DEFAULT_BACKOFF_SECONDS,
    label: str = "",
) -> T:
    """
    Call fn(); on rate-limit errors wait 1s → 3s → 9s… then retry.
    Non-rate-limit errors raise immediately.
    """
    attempts = len(backoff_seconds) + 1
    last_exc: BaseException | None = None
    what = label or platform

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if not is_rate_limit_error(e) or attempt >= attempts:
                raise
            wait = backoff_seconds[attempt - 1]
            # Small jitter so parallel-ish clients don't sync-thump the API.
            wait = wait + random.uniform(0, 0.5)
            print(
                f"[{platform}] Rate limited (429) on {what} — "
                f"waiting {wait:.1f}s then retry "
                f"({attempt}/{attempts - 1})…"
            )
            time.sleep(wait)

    assert last_exc is not None
    raise last_exc
