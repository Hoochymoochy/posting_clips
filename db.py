#!/usr/bin/env python3
"""
db.py — Supabase helpers for the clip poster queue worker.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

_client = None


def get_supabase():
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
    )
    if not url or not key:
        raise RuntimeError(
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_ANON_KEY) in .env"
        )

    from supabase import create_client

    _client = create_client(url, key)
    return _client


def is_configured() -> bool:
    """Check whether Supabase environment variables are present."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
    )
    return bool(url and key)


def check_connection() -> tuple[bool, str | None]:
    """Test connection to Supabase database."""
    if not is_configured():
        return False, "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set."
    try:
        client = get_supabase()
        client.table("clips").select("id").limit(1).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def fetch_due_clips(limit: int = 10) -> list[dict[str, Any]]:
    """
    Clips ready to post:
      - posted = false
      - scheduled_at IS NULL  (post now / ASAP)
        OR scheduled_at <= now()
    """
    client = get_supabase()

    # PostgREST can't express (null OR <= now) in one filter easily via the
    # python client alone, so we fetch unposted clips and filter in Python.
    result = (
        client.table("clips")
        .select(
            "id, youtube_url, youtube_video_id, title, caption, tags, "
            "start_time, end_time, storage_url, posted, scheduled_at, created_at, "
            "channels(id, platform, status, post_url, error_message)"
        )
        .eq("posted", False)
        .order("created_at", desc=False)
        .limit(max(limit * 5, 50))
        .execute()
    )

    due: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for clip in result.data or []:
        scheduled = clip.get("scheduled_at")
        if scheduled is None:
            due.append(clip)
        else:
            try:
                dt = datetime.fromisoformat(str(scheduled).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= now:
                    due.append(clip)
            except ValueError:
                # Bad timestamp — treat as due so it doesn't sit forever
                due.append(clip)

    # ASAP (null) first, then earliest scheduled_at
    def sort_key(c: dict[str, Any]):
        s = c.get("scheduled_at")
        if s is None:
            return (0, datetime.min.replace(tzinfo=timezone.utc))
        try:
            dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (1, dt)
        except ValueError:
            return (1, datetime.min.replace(tzinfo=timezone.utc))

    due.sort(key=sort_key)
    return due[:limit]


def get_clip_by_id(clip_id: str) -> dict[str, Any] | None:
    """Fetch a single clip by its UUID with associated channels."""
    client = get_supabase()
    res = (
        client.table("clips")
        .select(
            "id, youtube_url, youtube_video_id, title, caption, tags, "
            "start_time, end_time, storage_url, posted, scheduled_at, created_at, "
            "channels(id, platform, status, post_url, error_message)"
        )
        .eq("id", clip_id)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]
    return None


def _extract_youtube_video_id(url: str) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    try:
        parsed = urllib.parse.urlparse(raw if "://" in raw else f"https://{raw}")
        host = (parsed.netloc or "").lower().replace("www.", "")
        path = parsed.path or ""
        qs = urllib.parse.parse_qs(parsed.query or "")
        if host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
            if qs.get("v"):
                return qs["v"][0]
            m = re.match(r"^/(shorts|embed|live)/([A-Za-z0-9_-]{6,})", path)
            if m:
                return m.group(2)
        if host == "youtu.be":
            vid = path.lstrip("/").split("/")[0]
            return vid or None
    except Exception:
        return None
    return None


def register_clip(
    *,
    clip_id: str | None = None,
    youtube_url: str = "",
    storage_url: str | None = None,
    title: str | None = None,
    caption: str | None = None,
    tags: list[str] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    scheduled_at: Any = None,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    """
    Insert or upsert a clip record and its pending channel rows in Supabase.
    """
    client = get_supabase()

    video_id = _extract_youtube_video_id(youtube_url)
    row: dict[str, Any] = {
        "youtube_url": youtube_url or "https://youtube.com/shorts/local_upload",
        "youtube_video_id": video_id,
        "title": title or None,
        "caption": (caption or "").strip() or None,
        "tags": tags or None,
        "start_time": start_time or None,
        "end_time": end_time or None,
        "storage_url": storage_url or None,
        "posted": False,
    }
    if clip_id:
        row["id"] = clip_id

    if scheduled_at:
        if isinstance(scheduled_at, datetime):
            row["scheduled_at"] = (
                scheduled_at if scheduled_at.tzinfo else scheduled_at.replace(tzinfo=timezone.utc)
            ).isoformat()
        else:
            s_val = str(scheduled_at).strip()
            if s_val and s_val.lower() not in ("now", "immediate", "asap"):
                row["scheduled_at"] = s_val

    clip_res = client.table("clips").upsert(row).execute()
    if not clip_res.data:
        raise RuntimeError("Failed to upsert clip row into Supabase.")
    clip = clip_res.data[0]
    target_id = clip["id"]

    allowed = {"youtube", "instagram", "tiktok"}
    platforms = [p.lower().strip() for p in (platforms or []) if p]
    platforms = [p for p in platforms if p in allowed]
    if not platforms:
        platforms = ["youtube", "instagram", "tiktok"]

    channel_rows = [
        {
            "clip_id": target_id,
            "platform": p,
            "status": "pending",
        }
        for p in platforms
    ]
    channels_res = client.table("channels").upsert(channel_rows, on_conflict="clip_id,platform").execute()

    return {
        "clip": clip,
        "channels": list(channels_res.data or []),
    }


# Terminal statuses — clip can be marked posted once every channel is one of these
TERMINAL_STATUSES = frozenset({"success", "failed", "uncertain"})
# Statuses that must never be auto-retried (anti double-upload)
NO_RETRY_STATUSES = frozenset({"success", "uncertain", "processing"})


def update_channel(
    channel_id: str,
    *,
    status: str,
    post_url: str | None = None,
    error_message: str | None = None,
) -> None:
    client = get_supabase()
    payload: dict[str, Any] = {"status": status}
    if status == "success":
        payload["posted_at"] = datetime.now(timezone.utc).isoformat()
        payload["error_message"] = None
        if post_url:
            payload["post_url"] = post_url
    elif status in ("failed", "uncertain"):
        msg = error_message or "Unknown error"
        if status == "uncertain" and not msg.upper().startswith("[UNCERTAIN]"):
            msg = f"[UNCERTAIN] {msg}"
        payload["error_message"] = msg[:2000]
        if post_url:
            payload["post_url"] = post_url
    elif status == "pending":
        payload["error_message"] = None

    try:
        client.table("channels").update(payload).eq("id", channel_id).execute()
    except Exception as e:
        # Some projects constrain status to pending|success|failed only
        err = str(e).lower()
        if status in ("uncertain", "processing") and (
            "check" in err or "constraint" in err or "invalid" in err or "enum" in err
        ):
            fallback = "failed" if status == "uncertain" else "pending"
            payload["status"] = fallback
            if status == "uncertain":
                payload["error_message"] = (payload.get("error_message") or "[UNCERTAIN]")[:2000]
            print(f"  [WARN] status={status!r} rejected by DB; wrote {fallback!r} instead ({e})")
            client.table("channels").update(payload).eq("id", channel_id).execute()
        else:
            raise


def claim_channel(channel_id: str, *, from_statuses: list[str] | None = None) -> bool:
    """
    Best-effort claim: move channel to 'processing' so concurrent workers skip it.
    Falls back to a local lock if the DB rejects the processing status.
    """
    from retry_state import try_local_claim

    client = get_supabase()
    allowed = from_statuses or ["pending", "failed"]
    try:
        res = (
            client.table("channels")
            .update({"status": "processing"})
            .eq("id", channel_id)
            .in_("status", allowed)
            .execute()
        )
        if res.data:
            return True
        # Empty data can mean 0 rows matched (already claimed) OR no RETURNING —
        # verify current status.
        check = (
            client.table("channels")
            .select("id, status")
            .eq("id", channel_id)
            .limit(1)
            .execute()
        )
        row = (check.data or [None])[0]
        if row and (row.get("status") or "").lower() == "processing":
            return True
        return False
    except Exception as e:
        print(f"  [WARN] DB claim failed ({e}); using local claim lock.")
        return try_local_claim(channel_id)


def mark_clip_posted(clip_id: str, posted: bool = True) -> None:
    client = get_supabase()
    client.table("clips").update({"posted": posted}).eq("id", clip_id).execute()


def pending_channels(clip: dict[str, Any]) -> list[dict[str, Any]]:
    channels = clip.get("channels") or []
    return [c for c in channels if c.get("status") == "pending"]


def actionable_channels(clip: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Channels the worker may act on now:
      - pending (first try or reset for retry)
      - failed that are still retryable (checked via retry_state)
      - processing that appear stuck (reset candidate)
    Never includes success / uncertain.
    """
    from retry_state import is_retryable, is_stuck_processing

    out: list[dict[str, Any]] = []
    for ch in clip.get("channels") or []:
        status = (ch.get("status") or "").lower()
        cid = str(ch.get("id") or "")
        if not cid:
            continue
        if status == "pending":
            out.append(ch)
        elif status == "failed" and is_retryable(cid, status):
            out.append(ch)
        elif status == "processing" and is_stuck_processing(cid, status):
            out.append(ch)
    return out


def all_channels_terminal(clip: dict[str, Any]) -> bool:
    """
    True when no channel still needs work.
    Failed channels that still have retry budget are NOT terminal.
    """
    from retry_state import has_scheduled_retry, is_retryable

    channels = clip.get("channels") or []
    if not channels:
        return True
    for c in channels:
        status = (c.get("status") or "").lower()
        cid = str(c.get("id") or "")
        if status in ("pending", "processing"):
            return False
        if status == "failed" and cid and (
            is_retryable(cid, "failed") or has_scheduled_retry(cid, "failed")
        ):
            return False
        if status not in TERMINAL_STATUSES:
            return False
    return True


def refresh_clip_channels(clip_id: str) -> dict[str, Any] | None:
    """Re-fetch clip with channels after status updates."""
    return get_clip_by_id(clip_id)
