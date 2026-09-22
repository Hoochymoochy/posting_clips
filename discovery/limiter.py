"""
discovery/limiter.py — Daily-cap + once-per-URL-per-day tracking for discovery runs.

Uses the project's existing Supabase client (pass get_supabase() from db.py).
Failed runs do not consume quota and do not block a same-day retry for that URL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Easy to change later.
MAX_RUNS_PER_DAY = 3

_QUOTA_STATUSES = ("pending", "success")


class RunBlockedError(Exception):
    """Raised when create_run is not allowed (daily cap or URL already ran today)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# Back-compat alias
DailyCapReachedError = RunBlockedError


def _start_of_today_utc() -> str:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()


def _count_quota_rows(supabase, *, youtube_url: str | None = None) -> int:
    """Count pending/success rows since start of today UTC, optionally for one URL."""
    start = _start_of_today_utc()
    query = (
        supabase.table("discovery_runs")
        .select("id", count="exact")
        .gte("created_at", start)
        .in_("status", list(_QUOTA_STATUSES))
    )
    if youtube_url is not None:
        query = query.eq("youtube_url", youtube_url)
    result = query.execute()
    return result.count if result.count is not None else len(result.data or [])


def can_create_run(
    supabase,
    youtube_url: str | None = None,
) -> tuple[bool, str | None]:
    """
    Return whether a new discovery run is allowed.

    Checks (in order, when youtube_url is provided):
      1. This URL already has a pending/success run today (UTC) →
         (False, "url_already_ran_today")
      2. Today's pending+success count >= MAX_RUNS_PER_DAY →
         (False, "daily_cap_reached")

    Failed runs do not count for either check (same-day retry allowed after failure).
    """
    if youtube_url is not None:
        if _count_quota_rows(supabase, youtube_url=youtube_url) > 0:
            return False, "url_already_ran_today"

    if _count_quota_rows(supabase) >= MAX_RUNS_PER_DAY:
        return False, "daily_cap_reached"

    return True, None


def create_run(supabase, youtube_url: str) -> dict[str, Any]:
    """Insert a pending discovery_runs row, or raise RunBlockedError."""
    ok, reason = can_create_run(supabase, youtube_url)
    if not ok:
        raise RunBlockedError(reason or "blocked")

    result = (
        supabase.table("discovery_runs")
        .insert(
            {
                "youtube_url": youtube_url,
                "status": "pending",
            }
        )
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to insert discovery_runs row.")
    return result.data[0]


def mark_run_success(supabase, run_id: str) -> None:
    """Mark a discovery run as success."""
    now = datetime.now(timezone.utc).isoformat()
    (
        supabase.table("discovery_runs")
        .update({"status": "success", "updated_at": now})
        .eq("id", run_id)
        .execute()
    )


def mark_run_failed(supabase, run_id: str) -> None:
    """Mark a discovery run as failed (does not consume daily quota)."""
    now = datetime.now(timezone.utc).isoformat()
    (
        supabase.table("discovery_runs")
        .update({"status": "failed", "updated_at": now})
        .eq("id", run_id)
        .execute()
    )
