"""Discovery run tracking (daily limit + once-per-URL-per-day)."""

from discovery.limiter import (
    MAX_RUNS_PER_DAY,
    DailyCapReachedError,
    RunBlockedError,
    can_create_run,
    create_run,
    mark_run_failed,
    mark_run_success,
)

__all__ = [
    "MAX_RUNS_PER_DAY",
    "DailyCapReachedError",
    "RunBlockedError",
    "can_create_run",
    "create_run",
    "mark_run_failed",
    "mark_run_success",
]
