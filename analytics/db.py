#!/usr/bin/env python3
"""db.py — Supabase helpers for analytics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import ANALYTICS_PLATFORMS, get_settings

_client = None


def get_supabase():
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    url = settings["supabase_url"]
    key = settings["supabase_key"]
    if not url or not key:
        raise RuntimeError(
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in posting_clips/.env"
        )

    from supabase import create_client

    _client = create_client(url, key)
    return _client


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings["supabase_url"] and settings["supabase_key"])


def check_connection() -> tuple[bool, str | None]:
    if not is_configured():
        return False, "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set."
    try:
        client = get_supabase()
        client.table("clips").select("id").limit(1).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def _channel_select() -> str:
    return (
        "id, clip_id, platform, status, post_url, posted_at, "
        "views, likes, comments, shares, saves, last_analytics_at, "
        "clips(id, title, youtube_url, posted, total_views, total_likes, "
        "total_comments, total_shares, last_analytics_at)"
    )


def list_published_channels(
    *,
    clip_id: str | None = None,
    platform: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return channels with status=success and a non-empty post_url.
    Optionally filter by clip_id and/or platform.
    Always restricted to ANALYTICS_PLATFORMS.
    """
    enabled = [p.lower().strip() for p in ANALYTICS_PLATFORMS]
    if not enabled:
        return []

    client = get_supabase()
    query = (
        client.table("channels")
        .select(_channel_select())
        .eq("status", "success")
        .not_.is_("post_url", "null")
        .neq("post_url", "")
    )
    if clip_id:
        query = query.eq("clip_id", clip_id)

    if platform:
        p = platform.lower().strip()
        if p not in enabled:
            return []
        query = query.eq("platform", p)
    elif len(enabled) == 1:
        # Prefer eq for a single platform — avoids PostgREST in_() quirks.
        query = query.eq("platform", enabled[0])
    else:
        query = query.in_("platform", enabled)

    result = query.order("posted_at", desc=True).execute()
    rows = result.data or []
    return [
        r
        for r in rows
        if (r.get("post_url") or "").strip()
        and (r.get("platform") or "").lower().strip() in enabled
    ]

def get_clip_meta(clip_id: str) -> dict[str, Any] | None:
    client = get_supabase()
    res = (
        client.table("clips")
        .select("id, title, youtube_url, posted, created_at")
        .eq("id", clip_id)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]
    return None


def insert_analytics_snapshot(
    *,
    clip_id: str,
    channel_id: str | None,
    platform: str,
    post_url: str | None,
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    saves: int = 0,
    watch_time_seconds: float | None = None,
    raw_metrics: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    client = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "clip_id": clip_id,
        "channel_id": channel_id,
        "platform": platform,
        "post_url": post_url,
        "views": int(views or 0),
        "likes": int(likes or 0),
        "comments": int(comments or 0),
        "shares": int(shares or 0),
        "saves": int(saves or 0),
        "watch_time_seconds": watch_time_seconds or 0,
        "raw_metrics": raw_metrics or {},
        "error": error,
        "captured_at": now,
    }
    res = client.table("clip_analytics").insert(payload).execute()
    if res.data:
        return res.data[0]
    return payload


def update_channel_metrics(
    channel_id: str,
    *,
    views: int,
    likes: int,
    comments: int,
    shares: int,
    saves: int,
    average_watch_percent: float | None = None,
) -> None:
    client = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "views": int(views or 0),
        "likes": int(likes or 0),
        "comments": int(comments or 0),
        "shares": int(shares or 0),
        "saves": int(saves or 0),
        "last_analytics_at": now,
    }
    awp_value = None
    if average_watch_percent is not None:
        try:
            awp_value = float(average_watch_percent)
            payload["average_watch_percent"] = awp_value
        except (TypeError, ValueError):
            awp_value = None
    try:
        client.table("channels").update(payload).eq("id", channel_id).execute()
    except Exception as exc:
        # Column may not exist until migration 007 is applied.
        err = str(exc).lower()
        if awp_value is not None and (
            "average_watch_percent" in err or "column" in err or "schema" in err
        ):
            payload.pop("average_watch_percent", None)
            client.table("channels").update(payload).eq("id", channel_id).execute()
            print(
                f"  [WARN] channels.average_watch_percent missing; "
                f"ran migration 007? Wrote views/likes only ({exc})"
            )
        else:
            raise


def rollup_clip_totals(clip_id: str) -> None:
    """Sum latest denormalized channel metrics onto the parent clip."""
    client = get_supabase()
    res = (
        client.table("channels")
        .select("views, likes, comments, shares")
        .eq("clip_id", clip_id)
        .eq("status", "success")
        .execute()
    )
    rows = res.data or []
    totals = {
        "total_views": sum(int(r.get("views") or 0) for r in rows),
        "total_likes": sum(int(r.get("likes") or 0) for r in rows),
        "total_comments": sum(int(r.get("comments") or 0) for r in rows),
        "total_shares": sum(int(r.get("shares") or 0) for r in rows),
        "last_analytics_at": datetime.now(timezone.utc).isoformat(),
    }
    client.table("clips").update(totals).eq("id", clip_id).execute()
