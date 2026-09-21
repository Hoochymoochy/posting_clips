#!/usr/bin/env python3
"""
retry_state.py — Local attempt / backoff tracking for safe upload retries.

Keeps state on disk so we do not require a Supabase schema migration.
Anti-double-upload rules:
  - Never retry status success or uncertain
  - Never retry auth failures
  - Cap transient retries at MAX_ATTEMPTS
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

STATE_PATH = Path(os.environ.get("RETRY_STATE_FILE", ".retry_state.json"))
MAX_ATTEMPTS = int(os.environ.get("UPLOAD_MAX_ATTEMPTS", "3"))
# Backoff seconds between attempts: attempt 1→2, 2→3
BACKOFF_SECONDS = [
    int(x)
    for x in os.environ.get("UPLOAD_RETRY_BACKOFF", "60,300,900").split(",")
    if x.strip().isdigit()
] or [60, 300, 900]
STUCK_PROCESSING_SECONDS = int(os.environ.get("STUCK_PROCESSING_SECONDS", "1800"))


def _empty() -> dict[str, Any]:
    return {"channels": {}, "worker_heartbeat": 0.0, "supabase_fail_streak": 0}


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return _empty()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        data.setdefault("channels", {})
        data.setdefault("worker_heartbeat", 0.0)
        data.setdefault("supabase_fail_streak", 0)
        return data
    except Exception:
        return _empty()


def save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  [WARN] Could not save retry state: {e}")


def channel_entry(state: dict[str, Any], channel_id: str) -> dict[str, Any]:
    channels = state.setdefault("channels", {})
    entry = channels.get(channel_id)
    if not isinstance(entry, dict):
        entry = {
            "attempts": 0,
            "last_attempt_at": 0.0,
            "next_retry_at": 0.0,
            "last_kind": None,
            "claimed_at": 0.0,
            "share_clicked": False,
        }
        channels[channel_id] = entry
    return entry


def record_heartbeat(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state = state if state is not None else load_state()
    state["worker_heartbeat"] = time.time()
    save_state(state)
    return state


def get_attempts(channel_id: str) -> int:
    return int(channel_entry(load_state(), channel_id).get("attempts") or 0)


def mark_share_clicked(channel_id: str) -> None:
    """Once Share/Post is clicked, auto-retry is unsafe even if unconfirmed."""
    state = load_state()
    entry = channel_entry(state, channel_id)
    entry["share_clicked"] = True
    save_state(state)


def begin_attempt(channel_id: str) -> int:
    """Increment attempt counter and stamp claim time. Returns new attempt number."""
    state = load_state()
    entry = channel_entry(state, channel_id)
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["last_attempt_at"] = time.time()
    entry["claimed_at"] = time.time()
    entry["next_retry_at"] = 0.0
    save_state(state)
    return int(entry["attempts"])


def finish_attempt(
    channel_id: str,
    *,
    kind: str,
    share_clicked: bool = False,
) -> dict[str, Any]:
    """
    Record outcome. Schedules next_retry_at for retryable transient failures.
    Returns the channel entry.
    """
    state = load_state()
    entry = channel_entry(state, channel_id)
    entry["last_kind"] = kind
    entry["claimed_at"] = 0.0
    if share_clicked or kind == "uncertain":
        entry["share_clicked"] = True

    attempts = int(entry.get("attempts") or 0)
    retryable = (
        kind == "transient"
        and attempts < MAX_ATTEMPTS
        and not entry.get("share_clicked")
    )
    if retryable:
        # backoff index: after attempt 1 use BACKOFF[0], etc.
        idx = min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)
        entry["next_retry_at"] = time.time() + BACKOFF_SECONDS[idx]
    else:
        entry["next_retry_at"] = 0.0

    save_state(state)
    return entry


def is_retryable(channel_id: str, status: str) -> bool:
    """True if a failed channel may be attempted again *right now*."""
    if status not in ("failed", "pending"):
        return False
    state = load_state()
    entry = channel_entry(state, channel_id)
    if entry.get("share_clicked") or entry.get("last_kind") in ("auth", "uncertain", "permanent"):
        return False
    attempts = int(entry.get("attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        return False
    if status == "pending" and attempts == 0:
        return True
    next_at = float(entry.get("next_retry_at") or 0)
    if status == "failed":
        return attempts > 0 and time.time() >= next_at
    # pending with prior attempts (reset for retry)
    return time.time() >= next_at


def has_scheduled_retry(channel_id: str, status: str) -> bool:
    """
    True if this channel still has retry budget (even during backoff).
    Used so we do not mark the clip posted while waiting to retry.
    """
    if status != "failed":
        return False
    state = load_state()
    entry = channel_entry(state, channel_id)
    if entry.get("share_clicked") or entry.get("last_kind") in ("auth", "uncertain", "permanent"):
        return False
    attempts = int(entry.get("attempts") or 0)
    if attempts <= 0 or attempts >= MAX_ATTEMPTS:
        return False
    return entry.get("last_kind") == "transient" or float(entry.get("next_retry_at") or 0) > 0


def is_stuck_processing(channel_id: str, status: str) -> bool:
    if status != "processing":
        return False
    entry = channel_entry(load_state(), channel_id)
    claimed = float(entry.get("claimed_at") or 0)
    if claimed <= 0:
        return True  # processing with no claim stamp — treat as stuck
    return (time.time() - claimed) >= STUCK_PROCESSING_SECONDS


def try_local_claim(channel_id: str, ttl_seconds: int | None = None) -> bool:
    """
    File-backed mutex when DB 'processing' status is unavailable.
    Returns False if another worker holds a fresh claim.
    """
    ttl = ttl_seconds if ttl_seconds is not None else STUCK_PROCESSING_SECONDS
    state = load_state()
    entry = channel_entry(state, channel_id)
    claimed_at = float(entry.get("claimed_at") or 0)
    if claimed_at > 0 and (time.time() - claimed_at) < ttl:
        return False
    entry["claimed_at"] = time.time()
    save_state(state)
    return True


def clear_channel(channel_id: str) -> None:
    state = load_state()
    state.get("channels", {}).pop(channel_id, None)
    save_state(state)


def bump_supabase_fail() -> int:
    state = load_state()
    state["supabase_fail_streak"] = int(state.get("supabase_fail_streak") or 0) + 1
    save_state(state)
    return int(state["supabase_fail_streak"])


def reset_supabase_fail() -> None:
    state = load_state()
    if state.get("supabase_fail_streak"):
        state["supabase_fail_streak"] = 0
        save_state(state)


def worker_heartbeat_age() -> float | None:
    state = load_state()
    hb = float(state.get("worker_heartbeat") or 0)
    if hb <= 0:
        return None
    return time.time() - hb
