"""Approve / decline discovery candidates — GPU + Ollama run on this host."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any


def _safe_stem(text: str, *, fallback: str = "clip") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (text or "").strip())[:40].strip("_")
    return cleaned or fallback


def _extra_comments_from_candidate(candidate: dict[str, Any]) -> tuple[str, list[str]]:
    comments = candidate.get("source_comments") or []
    if not isinstance(comments, list):
        comments = []
    texts = [str(c.get("text") or "").strip() for c in comments if isinstance(c, dict)]
    texts = [t for t in texts if t]
    primary = texts[0] if texts else (candidate.get("caption") or "")
    return primary, texts


def regenerate_caption(candidate: dict[str, Any]) -> str:
    from ollama_caption import rewrite_caption_with_ollama

    primary, extras = _extra_comments_from_candidate(candidate)
    result = rewrite_caption_with_ollama(
        primary,
        video_title=str(candidate.get("title") or ""),
        entry_timestamp=str(candidate.get("start_time") or ""),
        extra_comments=extras,
    )
    return (result.get("caption") or primary or candidate.get("title") or "New DJ clip").strip()


def approve_candidate(supabase, candidate_id: str) -> dict[str, Any]:
    """
    Mark approved. The discovery pipeline worker full-renders and registers the clip.
    Fast path for HTTP — heavy work is async in the discovery worker thread.
    """
    from discovery.candidates import get_candidate, update_candidate

    candidate = get_candidate(supabase, candidate_id)
    if not candidate:
        raise LookupError(f"Candidate {candidate_id} not found")
    if candidate.get("status") != "awaiting_review":
        raise ValueError(f"Candidate is not awaiting review (status={candidate.get('status')})")

    return update_candidate(
        supabase,
        candidate_id,
        status="approved",
        decline_reason=None,
    )


def decline_candidate(supabase, candidate_id: str, reason: str) -> dict[str, Any]:
    """
    clip_bad → rejected_clip
    caption_bad → Ollama rewrite on this host, stay awaiting_review
    """
    from discovery.candidates import get_candidate, update_candidate

    candidate = get_candidate(supabase, candidate_id)
    if not candidate:
        raise LookupError(f"Candidate {candidate_id} not found")
    if candidate.get("status") != "awaiting_review":
        raise ValueError(f"Candidate is not awaiting review (status={candidate.get('status')})")

    clean = (reason or "").strip().lower()
    if clean not in ("clip_bad", "caption_bad"):
        raise ValueError("reason must be clip_bad or caption_bad")

    if clean == "clip_bad":
        return update_candidate(
            supabase,
            candidate_id,
            status="rejected_clip",
            decline_reason="clip_bad",
        )

    new_caption = regenerate_caption(candidate)
    return update_candidate(
        supabase,
        candidate_id,
        status="awaiting_review",
        caption=new_caption,
        decline_reason="caption_bad",
    )


def full_render_and_queue(supabase, candidate_id: str) -> dict[str, Any]:
    """
    Full GPU/CPU render + register_clip into local clips/{id}/clip.mp4.
    Called by the discovery worker for status=approved.
    """
    from discovery.candidates import (
        get_candidate,
        next_scheduled_at,
        update_candidate,
    )
    from discovery.paths import workspace_dir
    from discovery.render import render_full
    from db import register_clip
    from worker import get_clips_dir

    candidate = get_candidate(supabase, candidate_id)
    if not candidate:
        raise LookupError(f"Candidate {candidate_id} not found")
    status = (candidate.get("status") or "").strip().lower()
    if status not in ("awaiting_review", "approved", "rendering"):
        raise ValueError(f"Candidate cannot be rendered (status={candidate.get('status')})")

    update_candidate(supabase, candidate_id, status="rendering", decline_reason=None)

    youtube_url = (candidate.get("youtube_url") or "").strip()
    start = candidate.get("start_time") or "00:00:00"
    end = candidate.get("end_time") or start
    title = (candidate.get("title") or "DJ Clip").strip()
    caption = (candidate.get("caption") or title).strip()

    stem = f"{_safe_stem(title)}_{_safe_stem(str(start))}_{str(uuid.uuid4())[:8]}"
    out_path = workspace_dir() / "renders" / f"{stem}_fullscreen.mp4"
    segment = candidate.get("segment_path")
    segment_path = Path(segment) if segment and Path(segment).is_file() else None

    try:
        render_full(
            youtube_url=youtube_url,
            start=start,
            end=end,
            title=title,
            out_path=out_path,
            segment_path=segment_path,
        )
        scheduled_at = next_scheduled_at(supabase)
        clip_id = str(uuid.uuid4())
        dest_folder = get_clips_dir() / clip_id
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_video = dest_folder / "clip.mp4"
        shutil.copy2(out_path, dest_video)

        storage_url = f"clips/{clip_id}/clip.mp4"
        db_result = register_clip(
            clip_id=clip_id,
            youtube_url=youtube_url,
            storage_url=storage_url,
            title=title,
            caption=caption,
            start_time=start,
            end_time=end,
            scheduled_at=scheduled_at,
        )
        updated = update_candidate(
            supabase,
            candidate_id,
            status="queued",
            clip_id=clip_id,
            scheduled_at=scheduled_at,
        )
        return {
            "candidate": updated,
            "clip_id": clip_id,
            "scheduled_at": scheduled_at,
            "video_path": str(dest_video),
            "supabase": db_result,
        }
    except Exception as exc:
        update_candidate(
            supabase,
            candidate_id,
            status="failed",
            error_message=str(exc)[:2000],
        )
        raise
