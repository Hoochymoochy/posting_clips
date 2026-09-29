"""Render Manual Clip Studio jobs into clips/{id}/clip.mp4 on this host.

Studio creates the Supabase row first (metadata + platforms). This module
downloads/renders the YouTube window and writes the local video the poster
worker expects.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_in_flight: set[str] = set()


def is_render_in_flight(clip_id: str) -> bool:
    with _lock:
        return clip_id in _in_flight


def _safe_stem(text: str, max_len: int = 40) -> str:
    import re

    cleaned = re.sub(r"[^\w\-]+", "_", (text or "").strip(), flags=re.UNICODE)
    cleaned = cleaned.strip("_") or "clip"
    return cleaned[:max_len]


def render_and_store_clip(clip_id: str, *, force: bool = False) -> dict[str, Any]:
    """
    Full-quality render for an existing clips row → clips/{id}/clip.mp4,
    then set storage_url on the Supabase row.
    """
    from db import get_clip_by_id, get_supabase, is_configured
    from discovery.paths import workspace_dir
    from discovery.render import render_full
    from ollama_caption import short_hook_overlay
    from worker import get_clips_dir

    clean_id = (clip_id or "").strip()
    if not clean_id:
        raise ValueError("clip_id is required")
    if not is_configured():
        raise RuntimeError("Supabase is not configured")

    with _lock:
        if clean_id in _in_flight:
            raise RuntimeError(f"Clip {clean_id} is already rendering")
        _in_flight.add(clean_id)

    try:
        clip = get_clip_by_id(clean_id)
        if not clip:
            raise LookupError(f"Clip {clean_id} not found in Supabase")

        dest_folder = get_clips_dir() / clean_id
        dest_video = dest_folder / "clip.mp4"
        if dest_video.is_file() and not force:
            storage_url = f"clips/{clean_id}/clip.mp4"
            if not clip.get("storage_url"):
                get_supabase().table("clips").update({"storage_url": storage_url}).eq(
                    "id", clean_id
                ).execute()
            return {
                "success": True,
                "id": clean_id,
                "skipped": True,
                "message": "Video already on disk",
                "storage_url": storage_url,
                "video_path": str(dest_video),
            }

        youtube_url = (clip.get("youtube_url") or "").strip()
        start = (clip.get("start_time") or "").strip() or "00:00:00"
        end = (clip.get("end_time") or "").strip() or start
        if not youtube_url:
            raise ValueError(f"Clip {clean_id} has no youtube_url to render from")
        if not clip.get("start_time") or not clip.get("end_time"):
            raise ValueError(f"Clip {clean_id} is missing start_time/end_time")

        caption = (clip.get("caption") or clip.get("title") or "DJ Clip").strip()
        overlay_title = short_hook_overlay(caption)

        stem = f"{_safe_stem(overlay_title)}_{_safe_stem(str(start))}_{uuid.uuid4().hex[:8]}"
        out_path = workspace_dir() / "renders" / f"{stem}_studio.mp4"

        print(f"  [studio-render] {clean_id} {youtube_url} [{start} → {end}]")
        render_full(
            youtube_url=youtube_url,
            start=start,
            end=end,
            title=overlay_title,
            out_path=out_path,
        )

        dest_folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, dest_video)

        storage_url = f"clips/{clean_id}/clip.mp4"
        get_supabase().table("clips").update({"storage_url": storage_url}).eq(
            "id", clean_id
        ).execute()

        print(f"  [studio-render] done {clean_id} -> {dest_video}")
        return {
            "success": True,
            "id": clean_id,
            "skipped": False,
            "message": f"Clip {clean_id} rendered and stored.",
            "storage_url": storage_url,
            "video_path": str(dest_video),
            "overlay_title": overlay_title,
        }
    finally:
        with _lock:
            _in_flight.discard(clean_id)


def render_and_store_clip_safe(clip_id: str, *, force: bool = False) -> None:
    """Background wrapper — never raises out of the thread."""
    try:
        render_and_store_clip(clip_id, force=force)
    except Exception as exc:
        print(f"  [studio-render] FAILED {clip_id}: {exc}")
