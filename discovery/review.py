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


def regenerate_caption(candidate: dict[str, Any]) -> tuple[str, str]:
    """
    Rewrite caption for a candidate. Reuses existing hook_archetype when set;
    otherwise selects a new one. Returns (caption, hook_archetype).
    """
    from hook_archetypes import select_archetype
    from ollama_caption import rewrite_caption_with_ollama

    primary, extras = _extra_comments_from_candidate(candidate)
    existing = (candidate.get("hook_archetype") or "").strip().lower()
    archetype = existing or select_archetype()
    result = rewrite_caption_with_ollama(
        primary,
        video_title=str(candidate.get("title") or ""),
        entry_timestamp=str(candidate.get("start_time") or ""),
        extra_comments=extras,
        hook_archetype=archetype,
    )
    caption = (result.get("caption") or primary or candidate.get("title") or "New DJ clip").strip()
    return caption, archetype


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

    new_caption, hook_archetype = regenerate_caption(candidate)
    return update_candidate(
        supabase,
        candidate_id,
        status="awaiting_review",
        caption=new_caption,
        hook_archetype=hook_archetype,
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
    set_title = (candidate.get("title") or "").strip()
    caption = (candidate.get("caption") or set_title or "DJ Clip").strip()
    hook_archetype = (candidate.get("hook_archetype") or "").strip().lower() or None

    from ollama_caption import short_hook_overlay

    # Burn the short hook into the short — never the full YouTube venue title.
    overlay_title = short_hook_overlay(caption)

    stem = f"{_safe_stem(overlay_title)}_{_safe_stem(str(start))}_{str(uuid.uuid4())[:8]}"
    out_path = workspace_dir() / "renders" / f"{stem}_fullscreen.mp4"
    segment = candidate.get("segment_path")
    segment_path = Path(segment) if segment and Path(segment).is_file() else None

    try:
        render_full(
            youtube_url=youtube_url,
            start=start,
            end=end,
            title=overlay_title,
            out_path=out_path,
            segment_path=segment_path,
        )
        scheduled_at = next_scheduled_at(supabase)
        clip_id = str(uuid.uuid4())
        dest_folder = get_clips_dir() / clip_id
        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_video = dest_folder / "clip.mp4"
        shutil.copy2(out_path, dest_video)

        # Prefer start/end delta; fall back to probing the rendered file.
        duration_seconds = None
        try:
            from discovery.youtube_meta import parse_timestamp_seconds

            s = parse_timestamp_seconds(start)
            e = parse_timestamp_seconds(end)
            if s is not None and e is not None and e > s:
                duration_seconds = round(float(e) - float(s), 2)
        except Exception:
            pass
        if duration_seconds is None:
            try:
                from uploader import get_video_info

                info = get_video_info(str(dest_video))
                if info.get("duration"):
                    duration_seconds = round(float(info["duration"]), 2)
            except Exception:
                pass

        storage_url = f"clips/{clip_id}/clip.mp4"
        db_result = register_clip(
            clip_id=clip_id,
            youtube_url=youtube_url,
            storage_url=storage_url,
            title=overlay_title,
            caption=caption,
            start_time=start,
            end_time=end,
            scheduled_at=scheduled_at,
            hook_archetype=hook_archetype,
            duration_seconds=duration_seconds,
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
