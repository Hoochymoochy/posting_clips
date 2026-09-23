"""discovery/candidates.py — Supabase helpers for discovery review candidates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from discovery.youtube_meta import normalize_clip_timestamp, normalize_youtube_url
from worker import get_clips_dir

DISCOVERY_CANDIDATE_STATUSES = (
    "previewing",
    "awaiting_review",
    "approved",
    "rejected_clip",
    "rejected_caption",
    "rendering",
    "queued",
    "failed",
    "needs_caption",
)

DEFAULT_POST_BUFFER_HOURS = float(
    __import__("os").environ.get("POST_BUFFER_HOURS", "2") or "2"
)


def discovery_preview_path(candidate_id: str) -> Path:
    folder = get_clips_dir() / "discovery" / str(candidate_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "preview.mp4"


def insert_candidate(
    supabase,
    *,
    youtube_url: str,
    discovery_run_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    title: str | None = None,
    caption: str | None = None,
    source_comments: list[dict[str, Any]] | None = None,
    preview_path: str | None = None,
    preview_url: str | None = None,
    segment_path: str | None = None,
    status: str = "previewing",
) -> dict[str, Any]:
    clean_status = (status or "previewing").strip().lower()
    if clean_status not in DISCOVERY_CANDIDATE_STATUSES:
        raise ValueError(f"Invalid discovery candidate status: {status!r}")

    row = {
        "discovery_run_id": discovery_run_id,
        "youtube_url": normalize_youtube_url(youtube_url),
        "start_time": normalize_clip_timestamp(start_time),
        "end_time": normalize_clip_timestamp(end_time),
        "title": (title or "").strip() or None,
        "caption": (caption or "").strip() or None,
        "source_comments": source_comments or [],
        "preview_path": preview_path,
        "preview_url": preview_url,
        "segment_path": segment_path,
        "status": clean_status,
    }
    result = supabase.table("discovery_candidates").insert(row).execute()
    if not result.data:
        raise RuntimeError("Failed to insert discovery_candidates row.")
    return result.data[0]


def list_candidates(
    supabase,
    *,
    status: str | list[str] | None = "awaiting_review",
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = supabase.table("discovery_candidates").select("*")
    if status is not None:
        if isinstance(status, str):
            query = query.eq("status", status)
        else:
            query = query.in_("status", list(status))
    result = (
        query.order("created_at", desc=False)
        .limit(max(1, min(int(limit), 200)))
        .execute()
    )
    return list(result.data or [])


def get_candidate(supabase, candidate_id: str) -> dict[str, Any] | None:
    result = (
        supabase.table("discovery_candidates")
        .select("*")
        .eq("id", candidate_id)
        .limit(1)
        .execute()
    )
    rows = list(result.data or [])
    return rows[0] if rows else None


def update_candidate(supabase, candidate_id: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    allowed = {
        "caption",
        "title",
        "preview_path",
        "preview_url",
        "segment_path",
        "status",
        "decline_reason",
        "clip_id",
        "scheduled_at",
        "error_message",
        "source_comments",
        "start_time",
        "end_time",
    }
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "status" and value is not None:
            clean = str(value).strip().lower()
            # needs_caption may not be in DB check yet — fall back to awaiting_review
            if clean == "needs_caption":
                payload["status"] = "awaiting_review"
                payload["decline_reason"] = fields.get("decline_reason") or "caption_bad_pending"
                continue
            if clean not in DISCOVERY_CANDIDATE_STATUSES:
                raise ValueError(f"Invalid discovery candidate status: {value!r}")
            payload["status"] = clean
        else:
            payload[key] = value

    if not payload:
        existing = get_candidate(supabase, candidate_id)
        if not existing:
            raise RuntimeError(f"Discovery candidate {candidate_id} was not found.")
        return existing

    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = (
        supabase.table("discovery_candidates")
        .update(payload)
        .eq("id", candidate_id)
        .execute()
    )
    if not result.data:
        raise RuntimeError(f"Failed to update discovery candidate {candidate_id}.")
    return result.data[0]


def public_candidate(row: dict[str, Any], *, api_base: str = "") -> dict[str, Any]:
    """Shape a candidate for the Vercel frontend (absolute preview URL)."""
    out = dict(row)
    cid = out.get("id")
    base = (api_base or "").rstrip("/")
    if cid:
        out["preview_url"] = f"{base}/api/review/{cid}/preview" if base else f"/api/review/{cid}/preview"
    return out


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def next_scheduled_at(
    supabase,
    *,
    buffer_hours: float | None = None,
    now: datetime | None = None,
) -> str:
    hours = DEFAULT_POST_BUFFER_HOURS if buffer_hours is None else float(buffer_hours)
    hours = max(0.0, hours)
    buffer = timedelta(hours=hours)
    anchor = now or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    else:
        anchor = anchor.astimezone(timezone.utc)

    latest = anchor
    try:
        rows = (
            supabase.table("clips")
            .select("scheduled_at, updated_at, posted")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        for row in rows.data or []:
            sched = _parse_iso_dt(row.get("scheduled_at"))
            if sched and sched > latest:
                latest = sched
            if row.get("posted"):
                posted_at = _parse_iso_dt(row.get("updated_at"))
                if (
                    posted_at
                    and (anchor - posted_at) <= buffer * 2
                    and posted_at > latest - buffer
                ):
                    latest = max(latest, posted_at)
    except Exception as exc:
        print(f"  [WARN] next_scheduled_at lookup failed: {exc}")

    return (max(latest, anchor) + buffer).isoformat()
