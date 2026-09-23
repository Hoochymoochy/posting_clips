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

# create_run / enqueue-style quota (rows that "own" a slot for the day).
_QUOTA_STATUSES = ("pending", "processing", "success")
# How many runs we may actively process per UTC day (claim gate).
_ACTIVE_STATUSES = ("processing", "success")


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
    """Count pending/processing/success rows since start of today UTC, optionally for one URL."""
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


def _count_active_today(supabase) -> int:
    """Count processing+success runs started today (UTC) — gates claiming work."""
    start = _start_of_today_utc()
    result = (
        supabase.table("discovery_runs")
        .select("id", count="exact")
        .gte("created_at", start)
        .in_("status", list(_ACTIVE_STATUSES))
        .execute()
    )
    return result.count if result.count is not None else len(result.data or [])


def can_create_run(
    supabase,
    youtube_url: str | None = None,
) -> tuple[bool, str | None]:
    """
    Return whether a new discovery run is allowed.

    Checks (in order, when youtube_url is provided):
      1. This URL already has a pending/processing/success run today (UTC) →
         (False, "url_already_ran_today")
      2. Today's pending+processing+success count >= MAX_RUNS_PER_DAY →
         (False, "daily_cap_reached")

    Failed runs do not count for either check (same-day retry allowed after failure).
    """
    if youtube_url is not None:
        if _count_quota_rows(supabase, youtube_url=youtube_url) > 0:
            return False, "url_already_ran_today"

    if _count_quota_rows(supabase) >= MAX_RUNS_PER_DAY:
        return False, "daily_cap_reached"

    return True, None


def can_claim_run(supabase) -> tuple[bool, str | None]:
    """True when today still has room for another processing/success run."""
    if _count_active_today(supabase) >= MAX_RUNS_PER_DAY:
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


def claim_next_pending_run(supabase) -> dict[str, Any] | None:
    """
    Atomically claim the oldest pending discovery_runs row (→ processing).

    Respects the daily active cap. Returns the claimed row, or None if none
    available / daily cap reached.
    """
    ok, _reason = can_claim_run(supabase)
    if not ok:
        return None

    result = (
        supabase.table("discovery_runs")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    rows = list(result.data or [])
    if not rows:
        return None

    run = rows[0]
    run_id = run["id"]
    now = datetime.now(timezone.utc).isoformat()
    updated = (
        supabase.table("discovery_runs")
        .update({"status": "processing", "updated_at": now})
        .eq("id", run_id)
        .eq("status", "pending")
        .execute()
    )
    if not updated.data:
        return None
    return updated.data[0]


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
