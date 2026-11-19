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
    Claim for local render immediately (status=rendering) so a remote discovery
    worker cannot race the same approval and fail on a foreign filesystem path.
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
        status="rendering",
        decline_reason=None,
        error_message=None,
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
    if clean == "clip_bad":
        return update_candidate(
            supabase,
            candidate_id,
            status="rejected_clip",
            decline_reason="clip_bad",
        )
    if clean != "caption_bad":
        raise ValueError("reason must be 'clip_bad' or 'caption_bad'")

    # caption_bad — regenerate caption, stay in review
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
    Full GPU/CPU template render + register_clip into local clips/{id}/.

    Writes one MP4 per active Discover template (named after the template),
    plus clip.mp4 as a copy of the first template output.

    Called by the discovery worker for status=approved.
    """
    import json

    from discovery.candidates import (
        get_candidate,
        update_candidate,
    )
    from discovery.paths import workspace_dir
    from discovery.render import (
        ensure_segment,
        render_variant_header,
        render_variant_intro,
    )
    from db import register_clip
    from templates.loader import (
        active_templates,
        load_template_config,
        template_intro_path,
    )
    from worker import get_clips_dir

    candidate = get_candidate(supabase, candidate_id)
    if not candidate:
        raise LookupError(f"Candidate {candidate_id} not found")
    status = (candidate.get("status") or "").strip().lower()
    if status not in ("awaiting_review", "approved", "rendering", "failed"):
        raise ValueError(f"Candidate cannot be rendered (status={candidate.get('status')})")

    update_candidate(
        supabase,
        candidate_id,
        status="rendering",
        decline_reason=None,
        error_message=None,
    )

    youtube_url = (candidate.get("youtube_url") or "").strip()
    start = candidate.get("start_time") or "00:00:00"
    end = candidate.get("end_time") or start
    set_title = (candidate.get("title") or "").strip()
    caption = (candidate.get("caption") or set_title or "DJ Clip").strip()
    hook_archetype = (candidate.get("hook_archetype") or "").strip().lower() or None

    from ollama_caption import short_hook_overlay

    # Burn the short hook into the short — never the full YouTube venue title.
    overlay_title = short_hook_overlay(caption)

    clip_id = str(uuid.uuid4())
    dest_folder = get_clips_dir() / clip_id
    dest_folder.mkdir(parents=True, exist_ok=True)
    dest_video = dest_folder / "clip.mp4"

    # Shared raw segment so we only hit YouTube once for all variants.
    segment = candidate.get("segment_path")
    segment_path = Path(segment) if segment and Path(segment).is_file() else None
    if segment_path is None:
        stem = f"{_safe_stem(overlay_title)}_{_safe_stem(str(start))}_{str(uuid.uuid4())[:8]}"
        segment_path = workspace_dir() / "segments" / f"{stem}.mp4"

    try:
        tpl_cfg = load_template_config()
        ensure_segment(
            youtube_url=youtube_url,
            start=start,
            end=end,
            segment_path=segment_path,
        )

        templates = active_templates()
        variant_meta: list[dict[str, Any]] = []
        variant_paths: dict[str, str] = {}

        if templates:
            for row in templates:
                tid = str(row.get("id") or "").strip()
                if not tid:
                    continue
                bumper = template_intro_path(tid)
                if not bumper.is_file():
                    continue
                key = f"tpl_{tid}"
                out = dest_folder / f"clip_{key}.mp4"
                label = str(row.get("name") or tid)
                print(f"  [templates] rendering “{label}” → {out}")
                render_variant_intro(
                    youtube_url=youtube_url,
                    start=start,
                    end=end,
                    title=overlay_title,
                    out_path=out,
                    segment_path=segment_path,
                    config=tpl_cfg,
                    bumper_path=bumper,
                )
                variant_meta.append(
                    {
                        "key": key,
                        "name": label,
                        "template_id": tid,
                        "file": out.name,
                    }
                )
                variant_paths[key] = str(out)

            # Back-compat aliases for older Clips UI / download links.
            first = dest_folder / variant_meta[0]["file"] if variant_meta else None
            if first and first.is_file():
                shutil.copy2(first, dest_folder / "clip_intro.mp4")
                shutil.copy2(first, dest_video)
                variant_paths["intro"] = str(dest_folder / "clip_intro.mp4")
                variant_paths["default"] = str(dest_video)
        else:
            # No active bumper templates — fall back to hook-only body.
            header_video = dest_folder / "clip_header.mp4"
            print(f"  [templates] no active templates — hook-only → {header_video}")
            render_variant_header(
                youtube_url=youtube_url,
                start=start,
                end=end,
                title=overlay_title,
                out_path=header_video,
                segment_path=segment_path,
                config=tpl_cfg,
            )
            shutil.copy2(header_video, dest_video)
            variant_meta.append(
                {
                    "key": "header",
                    "name": "Hook",
                    "template_id": None,
                    "file": header_video.name,
                }
            )
            variant_paths["header"] = str(header_video)
            variant_paths["default"] = str(dest_video)

        with open(dest_folder / "variants.json", "w", encoding="utf-8") as f:
            json.dump({"variants": variant_meta}, f, indent=2)
            f.write("\n")

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
        if duration_seconds is None and dest_video.is_file():
            try:
                from uploader import get_video_info

                info = get_video_info(str(dest_video))
                if info.get("duration"):
                    duration_seconds = round(float(info["duration"]), 2)
            except Exception:
                pass

        storage_url = f"clips/{clip_id}/clip.mp4"
        # Download-only: register the rendered file with no social channel rows
        # and no publish schedule (manual download from Studio / Discover).
        db_result = register_clip(
            clip_id=clip_id,
            youtube_url=youtube_url,
            storage_url=storage_url,
            title=overlay_title,
            caption=caption,
            start_time=start,
            end_time=end,
            scheduled_at=None,
            platforms=[],
            hook_archetype=hook_archetype,
            duration_seconds=duration_seconds,
        )
        updated = update_candidate(
            supabase,
            candidate_id,
            status="queued",
            clip_id=clip_id,
            scheduled_at=None,
            segment_path=str(segment_path),
            error_message=None,
        )
        return {
            "candidate": updated,
            "clip_id": clip_id,
            "scheduled_at": None,
            "video_path": str(dest_video),
            "variants": variant_paths,
            "variant_meta": variant_meta,
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
