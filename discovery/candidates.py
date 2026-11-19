"""discovery/candidates.py — Supabase helpers for discovery review candidates."""

from __future__ import annotations

import os
import random
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

# Untouched Discover suggestions older than this are auto-declined as expired.
DEFAULT_REVIEW_MAX_AGE_DAYS = 2

# Random gap from the last post/schedule (minute precision) to avoid exact cadence.
MIN_POST_BUFFER_HOURS = float(os.environ.get("POST_BUFFER_MIN_HOURS", "3") or "3")
MAX_POST_BUFFER_HOURS = float(os.environ.get("POST_BUFFER_MAX_HOURS", "6") or "6")


def get_review_max_age_days() -> float:
    raw = os.environ.get("DISCOVERY_REVIEW_MAX_AGE_DAYS")
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_REVIEW_MAX_AGE_DAYS)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(DEFAULT_REVIEW_MAX_AGE_DAYS)

# Optional fixed override. Prefer MIN/MAX for anti-automation jitter.
_fixed_buffer = os.environ.get("POST_BUFFER_HOURS")
DEFAULT_POST_BUFFER_HOURS = (
    float(_fixed_buffer) if _fixed_buffer not in (None, "") else None
)


def _random_post_buffer(
    *,
    buffer_hours: float | None = None,
    min_hours: float | None = None,
    max_hours: float | None = None,
) -> tuple[timedelta, timedelta]:
    """
    Pick the delay until the next post.

    Returns (delay, lookback) where lookback is the max gap used when scanning
    recent posts. Delay is randomized at minute granularity unless a fixed
    buffer_hours / POST_BUFFER_HOURS override is set.
    """
    if buffer_hours is not None:
        hours = max(0.0, float(buffer_hours))
        fixed = timedelta(hours=hours)
        return fixed, fixed

    if DEFAULT_POST_BUFFER_HOURS is not None:
        hours = max(0.0, float(DEFAULT_POST_BUFFER_HOURS))
        fixed = timedelta(hours=hours)
        return fixed, fixed

    lo = MIN_POST_BUFFER_HOURS if min_hours is None else float(min_hours)
    hi = MAX_POST_BUFFER_HOURS if max_hours is None else float(max_hours)
    lo = max(0.0, lo)
    hi = max(lo, hi)
    lo_minutes = int(round(lo * 60))
    hi_minutes = int(round(hi * 60))
    delay = timedelta(minutes=random.randint(lo_minutes, hi_minutes))
    lookback = timedelta(hours=hi)
    return delay, lookback


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
    hook_archetype: str | None = None,
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
        "hook_archetype": (hook_archetype or "").strip().lower() or None,
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
    # Newest suggestions first so the Discover deck stays on fresh sets.
    result = (
        query.order("created_at", desc=True)
        .limit(max(1, min(int(limit), 200)))
        .execute()
    )
    return list(result.data or [])


def expire_stale_awaiting_review(
    supabase,
    *,
    max_age_days: float | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """
    Auto-decline awaiting_review candidates older than DISCOVERY_REVIEW_MAX_AGE_DAYS.

    Keeps the Discover deck focused on fresh suggestions you still might look at.
    """
    days = get_review_max_age_days() if max_age_days is None else max(0.0, float(max_age_days))
    if days <= 0:
        return {"checked": 0, "expired": 0}

    anchor = now or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    else:
        anchor = anchor.astimezone(timezone.utc)
    cutoff = anchor - timedelta(days=days)

    result = (
        supabase.table("discovery_candidates")
        .select("id, created_at, status")
        .eq("status", "awaiting_review")
        .lt("created_at", cutoff.isoformat())
        .execute()
    )
    rows = list(result.data or [])
    expired = 0
    for row in rows:
        try:
            update_candidate(
                supabase,
                row["id"],
                status="rejected_clip",
                decline_reason="expired",
            )
            expired += 1
        except Exception as exc:
            print(f"  [WARN] failed to expire candidate {row.get('id')}: {exc}")

    if expired:
        print(f"  [discovery] expired {expired} stale awaiting_review candidate(s) (>{days}d)")
    return {"checked": len(rows), "expired": expired}


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
        "hook_archetype",
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


def _read_clip_variant_list(clip_id: str) -> list[dict[str, Any]]:
    """Named download variants for a rendered clip folder."""
    import json

    folder = get_clips_dir() / str(clip_id)
    meta_path = folder / "variants.json"
    if meta_path.is_file():
        try:
            with open(meta_path, encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("variants") if isinstance(data, dict) else None
            if isinstance(rows, list):
                out: list[dict[str, Any]] = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    key = str(row.get("key") or "").strip()
                    filename = str(row.get("file") or "").strip()
                    if not key:
                        continue
                    path = folder / filename if filename else folder / f"clip_{key}.mp4"
                    if path.is_file():
                        out.append(
                            {
                                "key": key,
                                "name": str(row.get("name") or key),
                                "template_id": row.get("template_id"),
                                "file": path.name,
                            }
                        )
                if out:
                    return out
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    out = []
    for path in sorted(folder.glob("clip_tpl_*.mp4")):
        key = path.stem.replace("clip_", "", 1)
        tid = key[4:] if key.startswith("tpl_") else key
        out.append({"key": key, "name": tid, "template_id": tid, "file": path.name})
    if (folder / "clip_intro.mp4").is_file() and not any(r["key"] == "intro" for r in out):
        label = "Intro"
        try:
            from templates.loader import active_templates

            active = active_templates()
            if active:
                label = str(active[0].get("name") or label)
        except Exception:
            pass
        out.append({"key": "intro", "name": label, "template_id": None, "file": "clip_intro.mp4"})
    if (folder / "clip_header.mp4").is_file() and not any(r["key"] == "header" for r in out):
        out.append({"key": "header", "name": "Hook", "template_id": None, "file": "clip_header.mp4"})
    if not out and (folder / "clip.mp4").is_file():
        out.append({"key": "default", "name": "Clip", "template_id": None, "file": "clip.mp4"})
    return out


def public_candidate(row: dict[str, Any], *, api_base: str = "") -> dict[str, Any]:
    """Shape a candidate for the Vercel frontend (absolute preview URL + variant downloads)."""
    out = dict(row)
    cid = out.get("id")
    clip_id = out.get("clip_id")
    base = (api_base or "").rstrip("/")
    if cid:
        path = f"/api/review/{cid}/preview?v=notitle"
        out["preview_url"] = f"{base}{path}" if base else path

    variants = {"header": False, "intro": False, "default": False}
    download_urls: dict[str, str] = {}
    variant_list: list[dict[str, Any]] = []
    if clip_id:
        folder = get_clips_dir() / str(clip_id)
        variants = {
            "default": (folder / "clip.mp4").is_file(),
            "header": (folder / "clip_header.mp4").is_file() or (folder / "clip.mp4").is_file(),
            "intro": (folder / "clip_intro.mp4").is_file(),
        }
        for item in _read_clip_variant_list(str(clip_id)):
            key = str(item.get("key") or "")
            if not key:
                continue
            variants[key] = True
            path = f"/api/clips/{clip_id}/download?variant={key}"
            url = f"{base}{path}" if base else path
            download_urls[key] = url
            variant_list.append(
                {
                    "key": key,
                    "name": item.get("name") or key,
                    "template_id": item.get("template_id"),
                    "url": url,
                }
            )
    out["variants"] = variants
    out["download_urls"] = download_urls
    out["variant_list"] = variant_list
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
    min_hours: float | None = None,
    max_hours: float | None = None,
    now: datetime | None = None,
) -> str:
    """
    Schedule the next clip a random 3–6 hours (incl. minutes) after the latest
    scheduled/posted clip, so posts do not land on a fixed cadence.
    """
    buffer, lookback = _random_post_buffer(
        buffer_hours=buffer_hours,
        min_hours=min_hours,
        max_hours=max_hours,
    )
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
                    and (anchor - posted_at) <= lookback * 2
                    and posted_at > latest - lookback
                ):
                    latest = max(latest, posted_at)
    except Exception as exc:
        print(f"  [WARN] next_scheduled_at lookup failed: {exc}")

    when = max(latest, anchor) + buffer
    print(
        f"  [schedule] next post at {when.isoformat()} "
        f"(+{buffer} from last activity)"
    )
    return when.isoformat()
