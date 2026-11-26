#!/usr/bin/env python3
"""
server.py — FastAPI backend server for clip uploads and background posting worker.

Features:
  1. Video ingestion API:
     - Stores received videos in: clips/[id]/clip.mp4
     - Caption / title / tags live in Supabase (not metadata.json)
  2. Background posting worker (ENABLE_BACKGROUND_WORKER, default off):
     - Polls Supabase for due clips and posts to social platforms
     - Download-only mode leaves this disabled; use Studio → Download instead
  3. Discovery pipeline (ENABLE_DISCOVERY_WORKER):
     - Claims discovery_runs, Ollama captions, GPU/CPU previews + full renders
     - Review APIs for the Vercel web app
  4. Analytics (ENABLE_ANALYTICS_WORKER):
     - Live YouTube / Instagram / TikTok metrics for published posts
     - GET /api/analytics/posts  (also /api/posts)
  5. Status & Health endpoints:
     - GET /health
      - GET /api/clips
      - POST /api/clips/{id}/retry/{platform}  (retry one failed platform)
      - GET /api/review/candidates
      - POST /api/review/{id}/approve|decline
      - POST /api/update-queue
      - POST /api/studio/comments|caption  (Manual Clip Studio)
      - POST /api/clips/{id}/render       (render YouTube window → clips/{id}/clip.mp4)
      - CRUD /api/projects                (editor project folders on disk)
"""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from auth_guard import require_youtube_import_user

from worker import (
    get_clips_dir,
    is_worker_running,
    run_loop,
    start_background_worker,
    stop_background_worker,
)
from logutil import init_logging

load_dotenv()
init_logging()

# Ensure UTF-8 on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Server configuration
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
HEADLESS = os.environ.get("HEADED", "false").lower() not in ("1", "true", "yes")
YOUTUBE_PRIVACY = os.environ.get("YOUTUBE_PRIVACY", "public")
# Download-only default: do not auto-post to socials unless explicitly re-enabled.
ENABLE_WORKER = os.environ.get("ENABLE_BACKGROUND_WORKER", "false").lower() in ("1", "true", "yes")
ENABLE_DISCOVERY = os.environ.get("ENABLE_DISCOVERY_WORKER", "true").lower() in ("1", "true", "yes")
DISCOVERY_POLL_SECONDS = int(os.environ.get("DISCOVERY_POLL_SECONDS", "120"))
ENABLE_ANALYTICS = os.environ.get("ENABLE_ANALYTICS_WORKER", "false").lower() in ("1", "true", "yes")
ANALYTICS_POLL_SECONDS = int(
    os.environ.get(
        "ANALYTICS_POLL_SECONDS",
        os.environ.get("POLL_INTERVAL_SECONDS", "3600"),
    )
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Launch background worker if enabled
    if ENABLE_WORKER:
        print(f"[Server] Starting background posting worker (poll every {POLL_SECONDS}s, dry_run={DRY_RUN})...")
        start_background_worker(
            poll_seconds=POLL_SECONDS,
            dry_run=DRY_RUN,
            headless=HEADLESS,
            privacy=YOUTUBE_PRIVACY,
            batch=BATCH_SIZE,
        )
    else:
        print("[Server] Background worker disabled via ENABLE_BACKGROUND_WORKER=false.")

    if ENABLE_DISCOVERY:
        from discovery.pipeline import start_discovery_worker

        print(
            f"[Server] Starting discovery pipeline "
            f"(GPU preview / Ollama / full render, poll every {DISCOVERY_POLL_SECONDS}s)..."
        )
        start_discovery_worker(poll_seconds=DISCOVERY_POLL_SECONDS)
    else:
        print("[Server] Discovery pipeline disabled via ENABLE_DISCOVERY_WORKER=false.")

    if ENABLE_ANALYTICS:
        from analytics.worker import start_background_worker as start_analytics_worker

        print(
            f"[Server] Starting analytics worker "
            f"(poll every {ANALYTICS_POLL_SECONDS}s)..."
        )
        start_analytics_worker(poll_seconds=ANALYTICS_POLL_SECONDS)
    else:
        print("[Server] Analytics worker disabled via ENABLE_ANALYTICS_WORKER=false.")

    yield

    # Shutdown
    if ENABLE_ANALYTICS:
        from analytics.worker import stop_background_worker as stop_analytics_worker

        print("[Server] Stopping analytics worker...")
        stop_analytics_worker()

    if ENABLE_DISCOVERY:
        from discovery.pipeline import stop_discovery_worker

        print("[Server] Stopping discovery pipeline...")
        stop_discovery_worker(timeout=5.0)

    if ENABLE_WORKER:
        print("[Server] Stopping background posting worker...")
        stop_background_worker(timeout=3.0)


app = FastAPI(
    title="Clip Poster Backend",
    description="Receives video clips and runs a background queue polling worker for social posting.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from analytics.routes import router as analytics_router  # noqa: E402

app.include_router(analytics_router)


@app.get("/api/creator-connections", tags=["Publishing"])
def api_creator_connections(
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    """Per-creator official publishing connection status (no secrets)."""
    from creator_credentials import public_connections

    oid = _request_owner_id(None, x_braindance_owner_id)
    return {"success": True, **public_connections(oid)}


CHUNK_SIZE = 1024 * 1024  # 1MB chunk size for video streaming


def _parse_tags(tags: Optional[str]) -> list[str] | None:
    if tags is None:
        return None
    items = [t.strip().lstrip("#") for t in tags.split(",") if t.strip()]
    return items or None


def _parse_platforms(platforms: Optional[str]) -> list[str] | None:
    if platforms is None:
        return None
    items = [p.strip().lower() for p in platforms.split(",") if p.strip()]
    return items or None


async def _save_clip_files(
    clip_id: str,
    video: UploadFile,
    caption: Optional[str] = None,
    title: Optional[str] = None,
    tags: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    platforms: Optional[str] = None,
    save_to_supabase: bool = False,
    youtube_url: Optional[str] = None,
) -> dict[str, Any]:
    """Stream video to clips/[id]/clip.mp4. Metadata lives in Supabase."""
    clips_dir = get_clips_dir()
    clip_folder = clips_dir / clip_id
    clip_folder.mkdir(parents=True, exist_ok=True)

    dest_video_path = clip_folder / "clip.mp4"

    # Stream video file in chunks to disk
    bytes_written = 0
    with open(dest_video_path, "wb") as f:
        while chunk := await video.read(CHUNK_SIZE):
            f.write(chunk)
            bytes_written += len(chunk)

    # Remove legacy metadata.json if present (Supabase is source of truth)
    legacy_meta = clip_folder / "metadata.json"
    if legacy_meta.is_file():
        try:
            legacy_meta.unlink()
        except OSError:
            pass

    tag_list = _parse_tags(tags)
    platform_list = _parse_platforms(platforms)
    storage_url = f"clips/{clip_id}/clip.mp4"

    db_result = None
    if save_to_supabase:
        try:
            from db import is_configured, register_clip

            if is_configured():
                db_result = register_clip(
                    clip_id=clip_id,
                    youtube_url=youtube_url or "",
                    storage_url=storage_url,
                    title=title,
                    caption=caption,
                    tags=tag_list,
                    scheduled_at=scheduled_at,
                    platforms=platform_list,
                )
            else:
                print("  [WARN] Supabase not configured; clip video saved locally only.")
        except Exception as e:
            print(f"  [WARN] Could not register clip in Supabase: {e}")

    print("  [INFO] Clip received:", clip_id, "->", dest_video_path, f"({bytes_written} bytes)")

    return {
        "success": True,
        "id": clip_id,
        "message": f"Clip {clip_id} received and stored.",
        "paths": {
            "folder": str(clip_folder),
            "video": str(dest_video_path),
        },
        "file_size_bytes": bytes_written,
        "storage_url": storage_url,
        "supabase": db_result,
    }


@app.get("/health", tags=["Health"])
@app.get("/", tags=["Health"])
def health_check():
    """Health and status check endpoint."""
    from alerts import EVENT_WORKER_DEAD, emit
    from connections import get_connections_status
    from db import check_connection
    from retry_state import worker_heartbeat_age

    db_ok, db_err = check_connection()
    clips_dir = get_clips_dir()
    connections = get_connections_status()

    total_clips = 0
    if clips_dir.is_dir():
        total_clips = len([d for d in clips_dir.iterdir() if d.is_dir()])

    hb_age = worker_heartbeat_age()
    worker_running = is_worker_running()
    # If worker is enabled but heartbeat is stale, fire a critical Discord alert
    stale_after = int(os.environ.get("WORKER_HEARTBEAT_STALE_SECONDS", "120"))
    if ENABLE_WORKER and (not worker_running or (hb_age is not None and hb_age > stale_after)):
        emit(
            EVENT_WORKER_DEAD,
            detail=(
                f"Background worker not healthy "
                f"(running={worker_running}, heartbeat_age_s={hb_age})"
            ),
            dedupe_key="worker:dead",
        )

    analytics_running = False
    analytics_creds: dict[str, Any] = {}
    try:
        from analytics.config import credential_status
        from analytics.worker import is_worker_running as analytics_worker_running

        analytics_running = analytics_worker_running()
        analytics_creds = credential_status()
    except Exception:
        pass

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "supabase": {
            "connected": db_ok,
            "error": db_err,
        },
        "background_worker": {
            "enabled": ENABLE_WORKER,
            "running": worker_running,
            "poll_seconds": POLL_SECONDS,
            "dry_run": DRY_RUN,
            "heartbeat_age_seconds": hb_age,
        },
        "analytics_worker": {
            "enabled": ENABLE_ANALYTICS,
            "running": analytics_running,
            "poll_seconds": ANALYTICS_POLL_SECONDS,
            "credentials": analytics_creds,
        },
        "storage": {
            "clips_dir": str(clips_dir),
            "total_clips_stored": total_clips,
        },
        "connections": {
            "youtube": connections["platforms"]["youtube"]["connected"],
            "instagram": connections["platforms"]["instagram"]["connected"],
            "tiktok": connections["platforms"]["tiktok"]["connected"],
            "all_connected": connections["all_connected"],
        },
    }


@app.post("/api/clips/{id}", tags=["Clips"])
@app.post("/clips/{id}", tags=["Clips"])
async def upload_clip_with_id(
    id: str,
    video: UploadFile = File(..., description="The MP4 clip file to save"),
    caption: Optional[str] = Form(None, description="Caption for the post"),
    title: Optional[str] = Form(None, description="Title for YouTube shorts"),
    tags: Optional[str] = Form(None, description="Comma-separated tags (e.g. DJ,EDM)"),
    scheduled_at: Optional[str] = Form(None, description="ISO timestamp or 'now'"),
    platforms: Optional[str] = Form(None, description="Comma-separated platforms: youtube,instagram,tiktok"),
    save_to_supabase: bool = Form(False, description="Whether to also register/upsert in Supabase"),
    youtube_url: Optional[str] = Form(None, description="Original source YouTube URL"),
):
    """Receive and store video at clips/[id]/clip.mp4. Metadata goes to Supabase when requested."""
    clean_id = id.strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="Clip id cannot be empty.")

    return await _save_clip_files(
        clip_id=clean_id,
        video=video,
        caption=caption,
        title=title,
        tags=tags,
        scheduled_at=scheduled_at,
        platforms=platforms,
        save_to_supabase=save_to_supabase,
        youtube_url=youtube_url,
    )


@app.post("/api/clips", tags=["Clips"])
@app.post("/clips", tags=["Clips"])
async def upload_clip(
    id: Optional[str] = Form(None, description="Optional clip id; generated if omitted"),
    video: UploadFile = File(..., description="The MP4 clip file to save"),
    caption: Optional[str] = Form(None, description="Caption for the post"),
    title: Optional[str] = Form(None, description="Title for YouTube shorts"),
    tags: Optional[str] = Form(None, description="Comma-separated tags"),
    scheduled_at: Optional[str] = Form(None, description="ISO timestamp or 'now'"),
    platforms: Optional[str] = Form(None, description="Comma-separated platforms"),
    save_to_supabase: bool = Form(False, description="Whether to also register/upsert in Supabase"),
    youtube_url: Optional[str] = Form(None, description="Original source YouTube URL"),
):
    """Upload endpoint allowing client to specify or auto-generate clip ID."""
    target_id = (id or "").strip() or str(uuid.uuid4())
    return await _save_clip_files(
        clip_id=target_id,
        video=video,
        caption=caption,
        title=title,
        tags=tags,
        scheduled_at=scheduled_at,
        platforms=platforms,
        save_to_supabase=save_to_supabase,
        youtube_url=youtube_url,
    )


@app.get("/api/clips/{id}", tags=["Clips"])
@app.get("/clips/{id}", tags=["Clips"])
def get_clip_info(id: str):
    """Check storage status for a specific clip ID (local files + Supabase channels)."""
    clips_dir = get_clips_dir()
    folder = clips_dir / id.strip()

    video_file = folder / "clip.mp4" if folder.is_dir() else None
    has_video = bool(video_file and video_file.is_file())
    video_size = video_file.stat().st_size if has_video else 0

    supabase_clip = None
    try:
        from db import get_clip_by_id, is_configured

        if is_configured():
            supabase_clip = get_clip_by_id(id.strip())
    except Exception as e:
        print(f"  [WARN] get_clip_by_id failed: {e}")

    if not has_video and not supabase_clip:
        raise HTTPException(status_code=404, detail=f"Clip {id} not found in {clips_dir} or Supabase.")

    channels = (supabase_clip or {}).get("channels") or []

    render_status = None
    render_error = None
    try:
        from studio_render import get_render_status, is_render_in_flight

        tracked = get_render_status(id.strip())
        if tracked:
            render_status = tracked.get("status")
            render_error = tracked.get("error")
        elif is_render_in_flight(id.strip()):
            render_status = "rendering"
        elif has_video:
            render_status = "ready"
    except Exception as e:
        print(f"  [WARN] render status lookup failed: {e}")

    return {
        "id": id,
        "folder": str(folder) if folder.is_dir() else None,
        "video_exists": has_video,
        "video_path": str(video_file) if has_video else None,
        "video_size_bytes": video_size,
        "storage_url": f"clips/{id}/clip.mp4" if has_video else (supabase_clip or {}).get("storage_url"),
        "title": (supabase_clip or {}).get("title"),
        "caption": (supabase_clip or {}).get("caption"),
        "posted": (supabase_clip or {}).get("posted"),
        "scheduled_at": (supabase_clip or {}).get("scheduled_at"),
        "channels": channels,
        "render_status": render_status,
        "render_error": render_error,
        "ready": has_video,
        "failed_platforms": [
            (c.get("platform") or "").lower()
            for c in channels
            if (c.get("status") or "").lower() in ("failed", "uncertain")
        ],
    }


def _clip_variants_meta(clip_id: str) -> list[dict]:
    from discovery.candidates import _read_clip_variant_list

    return _read_clip_variant_list(clip_id)


def _clip_mp4_path(clip_id: str, *, variant: str | None = None) -> Path | None:
    """Return path to a rendered MP4 for a clip id.

    Variants:
      - None / '' / 'default' → clip.mp4
      - 'header' → clip_header.mp4 (falls back to clip.mp4)
      - 'intro' → clip_intro.mp4
      - 'tpl_<id>' / template id → clip_tpl_<id>.mp4
    """
    import re

    folder = get_clips_dir() / clip_id.strip()
    clean = (variant or "").strip().lower()
    if clean in ("", "default", "clip"):
        names = ["clip.mp4"]
    elif clean == "header":
        names = ["clip_header.mp4", "clip.mp4"]
    elif clean == "intro":
        names = ["clip_intro.mp4"]
    elif re.fullmatch(r"tpl_[a-z0-9_-]+", clean):
        names = [f"clip_{clean}.mp4"]
    elif re.fullmatch(r"[a-z0-9_-]+", clean):
        names = [f"clip_tpl_{clean}.mp4", f"clip_{clean}.mp4"]
    else:
        return None
    for name in names:
        video = folder / name
        if video.is_file():
            return video
    return None


def _clip_variant_availability(clip_id: str) -> dict[str, bool]:
    folder = get_clips_dir() / clip_id.strip()
    avail = {
        "default": (folder / "clip.mp4").is_file(),
        "header": (folder / "clip_header.mp4").is_file() or (folder / "clip.mp4").is_file(),
        "intro": (folder / "clip_intro.mp4").is_file(),
    }
    for row in _clip_variants_meta(clip_id):
        avail[str(row["key"])] = True
    return avail


def _ensure_clip_mp3(clip_id: str) -> Path:
    """Extract clips/{id}/clip.mp3 from clip.mp4 (cached; rebuild if mp4 is newer)."""
    clean_id = clip_id.strip()
    mp4 = _clip_mp4_path(clean_id)
    if not mp4:
        raise HTTPException(status_code=404, detail=f"Rendered video not found for clip {clean_id}")

    mp3 = mp4.with_name("clip.mp3")
    if mp3.is_file() and mp3.stat().st_mtime >= mp4.stat().st_mtime:
        return mp3

    from render.main import extract_audio

    try:
        extract_audio(str(mp4), str(mp3), audio_format="mp3", title=clean_id)
    except Exception as e:
        if mp3.is_file():
            try:
                mp3.unlink()
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"MP3 extraction failed: {e}") from e

    if not mp3.is_file():
        raise HTTPException(status_code=500, detail="MP3 extraction produced no file.")
    return mp3


@app.get("/api/clips/{id}/download", tags=["Clips"])
@app.get("/clips/{id}/download", tags=["Clips"])
def download_clip_video(id: str, inline: bool = False, variant: str | None = None):
    """
    Stream the rendered MP4 for download (or inline playback when inline=true).

    ``variant``: header (default) | intro | default
    """
    clean_id = id.strip()
    clean_variant = (variant or "header").strip().lower() or "header"
    path = _clip_mp4_path(clean_id, variant=clean_variant)
    if not path:
        avail = _clip_variant_availability(clean_id)
        raise HTTPException(
            status_code=404,
            detail=(
                f"Rendered video not found for clip {clean_id} "
                f"(variant={clean_variant}, available={avail})"
            ),
        )

    disposition = "inline" if inline else "attachment"
    suffix = "" if clean_variant in ("header", "default", "clip", "") else f"_{clean_variant}"
    filename = f"{clean_id}{suffix}.mp4"
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=filename,
        content_disposition_type=disposition,
        headers={"Cache-Control": "private, max-age=60"},
    )


@app.get("/api/clips/{id}/download/audio", tags=["Clips"])
@app.get("/clips/{id}/download/audio", tags=["Clips"])
def download_clip_audio(id: str):
    """Extract audio from the rendered MP4 and stream it as MP3."""
    clean_id = id.strip()
    path = _ensure_clip_mp3(clean_id)
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=f"{clean_id}.mp3",
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, max-age=60"},
    )


@app.get("/clips/{id}/clip.mp4", tags=["Clips"])
def serve_clip_mp4(id: str):
    """Serve rendered MP4 at the canonical storage_url path (for <video> preview)."""
    clean_id = id.strip()
    path = _clip_mp4_path(clean_id)
    if not path:
        raise HTTPException(status_code=404, detail=f"Rendered video not found for clip {clean_id}")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{clean_id}.mp4",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=60"},
    )


@app.post("/api/clips/{id}/render", tags=["Clips"])
@app.post("/clips/{id}/render", tags=["Clips"])
def render_clip_route(
    id: str,
    force: bool = False,
    wait: bool = False,
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """
    Render a Studio/Supabase clip onto this host as clips/{id}/clip.mp4.

    Studio inserts the metadata row first; call this so the file is ready to
    download. By default runs in a background thread (returns immediately).

    Requires YouTube-import operator auth (JWT + allowlisted email).

    Query params:
      force=false  — re-render even if clip.mp4 already exists
      wait=false   — if true, block until render finishes (debug / curl)
    """
    import threading

    from db import get_clip_by_id, is_configured
    from studio_render import is_render_in_flight, render_and_store_clip, render_and_store_clip_safe

    clean_id = id.strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="Clip id cannot be empty.")
    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    clip = get_clip_by_id(clean_id)
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clean_id} not found in Supabase.")
    if not (clip.get("youtube_url") or "").strip():
        raise HTTPException(status_code=400, detail="Clip has no youtube_url.")
    if not clip.get("start_time") or not clip.get("end_time"):
        raise HTTPException(status_code=400, detail="Clip is missing start_time/end_time.")

    if is_render_in_flight(clean_id):
        return {
            "success": True,
            "id": clean_id,
            "status": "rendering",
            "message": "Render already in progress.",
        }

    clips_dir = get_clips_dir()
    existing = clips_dir / clean_id / "clip.mp4"
    if existing.is_file() and not force:
        storage_url = f"clips/{clean_id}/clip.mp4"
        if not clip.get("storage_url"):
            try:
                from db import get_supabase

                get_supabase().table("clips").update({"storage_url": storage_url}).eq(
                    "id", clean_id
                ).execute()
            except Exception as e:
                print(f"  [WARN] Could not set storage_url for {clean_id}: {e}")
        return {
            "success": True,
            "id": clean_id,
            "status": "ready",
            "skipped": True,
            "storage_url": storage_url,
            "message": "Video already on disk.",
        }

    if wait:
        try:
            result = render_and_store_clip(clean_id, force=force)
            return {**result, "status": "ready"}
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e

    threading.Thread(
        target=render_and_store_clip_safe,
        args=(clean_id,),
        kwargs={"force": force},
        daemon=True,
        name=f"studio-render-{clean_id}",
    ).start()
    return {
        "success": True,
        "id": clean_id,
        "status": "rendering",
        "message": "Render started in background. Video will appear under clips/{id}/clip.mp4.",
    }


@app.post("/api/clips/{id}/retry/{platform}", tags=["Clips"])
@app.post("/clips/{id}/retry/{platform}", tags=["Clips"])
def retry_clip_platform_route(
    id: str,
    platform: str,
    force: bool = False,
    immediate: bool = True,
):
    """
    Retry a single platform for a clip (e.g. YouTube failed → retry only YouTube).

    Query params:
      force=false     — required to retry success/uncertain/processing
      immediate=true  — run upload now; if false, just reset to pending for the worker

    Example: POST /api/clips/{id}/retry/youtube
    """
    from worker import retry_clip_platform

    try:
        return retry_clip_platform(
            id.strip(),
            platform,
            force=force,
            immediate=immediate,
            dry_run=DRY_RUN,
            headless=HEADLESS,
            privacy=YOUTUBE_PRIVACY,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/clips", tags=["Clips"])
@app.get("/clips", tags=["Clips"])
def list_clips():
    """List all received clips stored in clips/."""
    clips_dir = get_clips_dir()
    items = []

    if clips_dir.is_dir():
        for d in sorted(clips_dir.iterdir()):
            if not d.is_dir():
                continue
            clip_id = d.name
            vid = d / "clip.mp4"
            has_vid = vid.is_file()
            size = vid.stat().st_size if has_vid else 0

            items.append({
                "id": clip_id,
                "video_exists": has_vid,
                "video_size_bytes": size,
                "storage_url": f"clips/{clip_id}/clip.mp4" if has_vid else None,
            })

    return {
        "total": len(items),
        "clips": items,
    }


@app.delete("/api/clips/{id}", tags=["Clips"])
@app.delete("/clips/{id}", tags=["Clips"])
def delete_clip_files_route(id: str):
    """Remove clips/{id}/ from local disk. Does not touch Supabase metadata."""
    from worker import delete_clip_files

    try:
        return delete_clip_files(id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not delete clip files: {e}")


@app.post("/api/worker/poll-now", tags=["Worker"])
def trigger_poll_now():
    """Trigger one polling cycle synchronously or in a worker thread."""
    try:
        run_loop(
            once=True,
            poll_seconds=POLL_SECONDS,
            dry_run=DRY_RUN,
            headless=HEADLESS,
            privacy=YOUTUBE_PRIVACY,
            batch=BATCH_SIZE,
        )
        return {"success": True, "message": "Poll cycle completed successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/update-queue", tags=["Discovery"])
@app.post("/update-queue", tags=["Discovery"])
def update_discovery_queue(
    process: bool = True,
    max_runs: int = 1,
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """
    Cron-friendly: fetch DJ sets into discovery_runs, then optionally run the
    discovery worker (Ollama + GPU preview / approvals) on this host.

    Requires YouTube-import operator auth.

    Query params:
      process=true|false  (default true)
      max_runs=1          how many discovery_runs to claim after enqueue
    """
    from db import get_supabase, is_configured
    from discovery.queue import run_cron_cycle

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    try:
        result = run_cron_cycle(
            get_supabase(),
            process=process,
            max_runs=max(0, int(max_runs)),
        )
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _api_public_base() -> str:
    """Public base URL for absolute preview links (Vercel frontend)."""
    return (
        os.environ.get("PUBLIC_API_BASE_URL", "").strip().rstrip("/")
        or os.environ.get("CLIP_API_BASE_URL", "").strip().rstrip("/")
        or ""
    )


@app.get("/api/review/candidates", tags=["Review"])
def review_list_candidates(
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """List discovery candidates awaiting review, plus recent dual-render ready clips."""
    from db import get_supabase, is_configured
    from discovery.candidates import list_candidates, public_candidate

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    try:
        sb = get_supabase()
        rows = list_candidates(sb, status="awaiting_review", limit=50)
        # Hide ones waiting on caption regen so the deck doesn't show stale captions mid-job
        visible = [
            r for r in rows
            if (r.get("decline_reason") or "") != "caption_bad_pending"
        ]
        ready_rows = list_candidates(
            sb,
            status=["queued", "rendering", "approved", "failed"],
            limit=40,
        )
        # Drop ancient failures from the Clips status board (keep last 2 days).
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=2)

        def _fresh_enough(row: dict) -> bool:
            status = (row.get("status") or "").strip().lower()
            if status != "failed":
                return True
            raw = row.get("updated_at") or row.get("created_at")
            if not raw:
                return False
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc) >= cutoff
            except ValueError:
                return False

        ready_rows = [r for r in ready_rows if _fresh_enough(r)]
        base = _api_public_base()
        candidates = [public_candidate(r, api_base=base) for r in visible]
        ready = [public_candidate(r, api_base=base) for r in ready_rows]
        return {
            "success": True,
            "candidates": candidates,
            "count": len(candidates),
            "ready": ready,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/review/{candidate_id}", tags=["Review"])
def review_get_candidate(
    candidate_id: str,
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """Fetch one candidate (for post-approve status polling)."""
    from db import get_supabase, is_configured
    from discovery.candidates import get_candidate, public_candidate

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    row = get_candidate(get_supabase(), candidate_id.strip())
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {
        "success": True,
        "candidate": public_candidate(row, api_base=_api_public_base()),
    }


@app.get("/api/review/{candidate_id}/preview", tags=["Review"])
def review_preview_video(candidate_id: str):
    """
    Stream the cheap preview MP4 for a discovery candidate.

    Intentionally unauthenticated for <video src> playback (no Bearer header).
    Candidate listing / approve / decline remain operator-gated; IDs are not enumerated publicly.
    """
    from discovery.candidates import discovery_preview_path, get_candidate
    from db import get_supabase, is_configured

    path = discovery_preview_path(candidate_id)
    if not path.is_file():
        # Fall back to preview_path stored on the row (local generate machine path won't work
        # on a remote poster — upload endpoint should have written here).
        if is_configured():
            row = get_candidate(get_supabase(), candidate_id)
            alt = (row or {}).get("preview_path") or ""
            if alt and Path(alt).is_file():
                path = Path(alt)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Preview video not found")

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{candidate_id}_preview.mp4",
        headers={"Cache-Control": "private, no-cache"},
    )


@app.post("/api/review/rebuild-previews", tags=["Review"])
def review_rebuild_previews(
    limit: int = 50,
    status: str = "awaiting_review",
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """
    Re-render preview MP4s for the review deck without burned-in YouTube set titles.
    Run once after deploying preview render changes, or when old previews still show venue text.
    """
    from db import get_supabase, is_configured
    from discovery.preview_rebuild import rebuild_awaiting_review_previews

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    try:
        return rebuild_awaiting_review_previews(
            get_supabase(),
            limit=limit,
            status=(status or "awaiting_review").strip(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/review/{candidate_id}/preview", tags=["Review"])
async def upload_review_preview(
    candidate_id: str,
    video: UploadFile = File(...),
):
    """GPU worker uploads a preview MP4 after cheap render."""
    from db import get_supabase, is_configured
    from discovery.candidates import discovery_preview_path, get_candidate, update_candidate

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    sb = get_supabase()
    row = get_candidate(sb, candidate_id)
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")

    dest = discovery_preview_path(candidate_id)
    bytes_written = 0
    with open(dest, "wb") as f:
        while chunk := await video.read(CHUNK_SIZE):
            f.write(chunk)
            bytes_written += len(chunk)

    base = _api_public_base()
    preview_url = (
        f"{base}/api/review/{candidate_id}/preview?v=notitle"
        if base
        else f"/api/review/{candidate_id}/preview?v=notitle"
    )
    updated = update_candidate(
        sb,
        candidate_id,
        preview_path=str(dest),
        preview_url=preview_url,
        status="awaiting_review",
    )
    return {
        "success": True,
        "id": candidate_id,
        "file_size_bytes": bytes_written,
        "preview_url": preview_url,
        "candidate": updated,
    }


@app.post("/api/review/{candidate_id}/approve", tags=["Review"])
def review_approve(
    candidate_id: str,
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """Approve a preview and start full render on this host immediately."""
    import threading

    from db import get_supabase, is_configured
    from discovery.candidates import public_candidate
    from discovery.review import approve_candidate, full_render_and_queue
    from templates.loader import SELECT_TEMPLATE_MESSAGE, require_active_templates

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    # Don't pull the moment out of Discover if there's nothing to render with.
    try:
        require_active_templates()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e) or SELECT_TEMPLATE_MESSAGE)

    try:
        updated = approve_candidate(get_supabase(), candidate_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    cid = candidate_id.strip()

    def _render() -> None:
        try:
            full_render_and_queue(get_supabase(), cid)
            print(f"  [approve] full render done for {cid}")
        except Exception as exc:
            print(f"  [WARN] approve render failed for {cid}: {exc}")

    threading.Thread(target=_render, name=f"approve-render-{cid[:8]}", daemon=True).start()

    return {
        "success": True,
        "candidate": public_candidate(updated, api_base=_api_public_base()),
        "message": "Approved — rendering on this host now. Watch Clips for status.",
    }


@app.post("/api/review/{candidate_id}/decline", tags=["Review"])
def review_decline(
    candidate_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """Decline a preview (clip_bad or caption_bad)."""
    from db import get_supabase, is_configured
    from discovery.candidates import public_candidate
    from discovery.review import decline_candidate

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    body = payload or {}
    reason = str(body.get("reason") or "").strip().lower()
    try:
        updated = decline_candidate(get_supabase(), candidate_id, reason)
        regenerated = reason == "caption_bad"
        return {
            "success": True,
            "candidate": public_candidate(updated, api_base=_api_public_base()),
            "regenerated": regenerated,
            "regenerating": False,
            "message": (
                "Caption rewritten via Ollama."
                if regenerated
                else "Clip rejected."
            ),
        }
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/studio/comments", tags=["Studio"])
def studio_scan_comments(
    payload: dict[str, Any] | None = Body(default=None),
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """
    Manual Clip Studio: scrape timestamped YouTube comments for a URL and return
    clip-moment candidates (clustered by default, same logic as discovery).

    Requires YouTube-import operator auth.
    """
    body = payload or {}
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    mode = str(body.get("mode") or "cluster").strip().lower()
    top_n = max(1, min(int(body.get("top_n") or 8), 15))
    duration_sec = max(5, min(int(body.get("duration_sec") or 30), 60))
    max_comments = max(20, min(int(body.get("max_comments") or 400), 500))

    try:
        if mode == "rank":
            from discovery.comment_entries import find_comment_entry_points

            result = find_comment_entry_points(
                url,
                top_n=top_n,
                duration_sec=duration_sec,
                max_comments=max_comments,
            )
        else:
            from discovery.comment_entries import find_clustered_comment_entries

            result = find_clustered_comment_entries(
                url,
                top_n=top_n,
                duration_sec=duration_sec,
                max_comments=max_comments,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail=result.get("error") or "Comment scrape failed",
        )

    return {
        "success": True,
        "url": url,
        "mode": mode if mode in ("cluster", "rank") else "cluster",
        "entries": result.get("entries") or [],
        "scanned": result.get("scanned"),
        "video_title": result.get("video_title"),
        "video_duration": result.get("video_duration"),
        "duration_sec": result.get("duration_sec") or duration_sec,
        "message": result.get("message"),
    }


@app.post("/api/studio/caption", tags=["Studio"])
def studio_generate_caption(
    payload: dict[str, Any] | None = Body(default=None),
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """
    Manual Clip Studio: rewrite a fan comment (plus optional nearby comments)
    into a social caption via Ollama on this host.

    Requires YouTube-import operator auth.

    Optional ``custom_prompt`` / ``caption_prompt`` overrides the default voice
    (also auto-loaded from the first active template when omitted).
    """
    body = payload or {}
    comment_text = str(body.get("comment_text") or body.get("comment") or "").strip()
    video_title = str(body.get("video_title") or body.get("title") or "").strip()
    entry_timestamp = str(body.get("entry_timestamp") or body.get("start") or "").strip()
    extras_raw = body.get("extra_comments") or body.get("comments") or []
    if not isinstance(extras_raw, list):
        extras_raw = []
    extra_comments = [str(c).strip() for c in extras_raw if str(c).strip()]
    custom_prompt = str(
        body.get("custom_prompt") or body.get("caption_prompt") or ""
    ).strip() or None

    if not comment_text and not video_title:
        raise HTTPException(
            status_code=400,
            detail="comment_text or video_title is required",
        )

    try:
        from hook_archetypes import select_archetype
        from ollama_caption import rewrite_caption_with_ollama, short_hook_overlay

        if not custom_prompt:
            try:
                from templates.loader import active_caption_prompt

                custom_prompt = active_caption_prompt()
            except Exception:
                custom_prompt = None

        archetype = str(body.get("hook_archetype") or "").strip() or select_archetype()
        result = rewrite_caption_with_ollama(
            comment_text or video_title,
            video_title=video_title,
            entry_timestamp=entry_timestamp,
            extra_comments=extra_comments,
            hook_archetype=None if custom_prompt else archetype,
            custom_prompt=custom_prompt,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    caption = (result.get("caption") or comment_text or video_title or "").strip()
    hook_overlay = short_hook_overlay(caption)
    return {
        "success": bool(result.get("success")) or bool(caption),
        "caption": caption,
        "hook_overlay": hook_overlay,
        "fallback": bool(result.get("fallback")),
        "error": result.get("error"),
        "model": result.get("model"),
        "hook_archetype": result.get("hook_archetype") or result.get("archetype"),
        "custom_prompt": bool(custom_prompt),
        "source_comment": comment_text,
    }


# ---------------------------------------------------------------------------
# Editor projects (disk folders under projects/{id}/)
# ---------------------------------------------------------------------------


def _request_owner_id(
    owner_id: Optional[str] = None,
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
) -> Optional[str]:
    return (owner_id or x_braindance_owner_id or "").strip() or None


@app.get("/api/projects", tags=["Projects"])
def api_list_projects(
    owner_id: Optional[str] = None,
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from projects_store import filter_projects_for_owner, list_projects

    oid = _request_owner_id(owner_id, x_braindance_owner_id)
    return {"success": True, "projects": filter_projects_for_owner(list_projects(), oid)}


@app.post("/api/projects", tags=["Projects"])
def api_create_project(
    payload: dict[str, Any] | None = Body(default=None),
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from projects_store import create_project

    body = payload or {}
    oid = _request_owner_id(str(body.get("owner_id") or "") or None, x_braindance_owner_id)
    try:
        project = create_project(
            name=str(body.get("name") or "").strip() or None,
            description=str(body.get("description") or ""),
            source_clip_id=(str(body.get("source_clip_id") or body.get("clip_id") or "").strip() or None),
            copy_clip=bool(body.get("copy_clip", True)),
            timeline=body.get("timeline") if isinstance(body.get("timeline"), dict) else None,
            asset_name=str(body.get("asset_name") or "").strip() or None,
            owner_id=oid,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"success": True, "project": project}


@app.get("/api/projects/{project_id}", tags=["Projects"])
def api_get_project(
    project_id: str,
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from projects_store import assert_project_access, load_project

    try:
        project = load_project(project_id)
        assert_project_access(project, _request_owner_id(None, x_braindance_owner_id))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "project": project}


@app.patch("/api/projects/{project_id}", tags=["Projects"])
def api_patch_project(
    project_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from projects_store import assert_project_access, load_project, update_project

    body = payload or {}
    oid = _request_owner_id(None, x_braindance_owner_id)
    try:
        assert_project_access(load_project(project_id), oid)
        project = update_project(project_id, body)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "project": project}


@app.delete("/api/projects/{project_id}", tags=["Projects"])
def api_delete_project(
    project_id: str,
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from projects_store import assert_project_access, delete_project, load_project

    try:
        assert_project_access(load_project(project_id), _request_owner_id(None, x_braindance_owner_id))
        delete_project(project_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True}


@app.post("/api/projects/{project_id}/assets", tags=["Projects"])
async def api_upload_project_asset(
    project_id: str,
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    duration: float = Form(default=0),
    width: int = Form(default=0),
    height: int = Form(default=0),
):
    from projects_store import add_asset_bytes

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        asset = add_asset_bytes(
            project_id,
            raw=raw,
            filename=file.filename or "upload.bin",
            content_type=file.content_type,
            name=name,
            duration=duration,
            width=width,
            height=height,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "asset": asset}


@app.delete("/api/projects/{project_id}/assets/{asset_id}", tags=["Projects"])
def api_delete_project_asset(project_id: str, asset_id: str):
    from projects_store import delete_asset

    try:
        project = delete_asset(project_id, asset_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, "project": project}


@app.get("/api/projects/{project_id}/assets/{asset_id}", tags=["Projects"])
def api_stream_project_asset(project_id: str, asset_id: str):
    from projects_store import resolve_asset_path

    try:
        path = resolve_asset_path(project_id, asset_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    suffix = path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }
    return FileResponse(
        path,
        media_type=media_types.get(suffix, "application/octet-stream"),
        filename=path.name,
        content_disposition_type="inline",
    )


@app.get("/api/templates", tags=["Templates"])
def get_templates_status(
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    """List Discover templates (multi-template library)."""
    from templates.loader import (
        filter_templates_for_owner,
        list_templates,
        load_template_config,
        templates_dir,
    )

    try:
        cfg = load_template_config()
        oid = _request_owner_id(None, x_braindance_owner_id)
        templates = filter_templates_for_owner(list_templates(), oid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    ready = [t for t in templates if t.get("ready")]
    active = [t for t in templates if t.get("active") and t.get("ready")]
    # Back-compat fields for older UI
    first = templates[0] if templates else None
    return {
        "success": True,
        "templates": templates,
        "count": len(templates),
        "intro": len(ready) > 0,
        "intro_exists": len(ready) > 0,
        "intro_bytes": (ready[0].get("intro_bytes") if ready else 0),
        "editor_project_id": (first or {}).get("project_id"),
        "active_ids": [t["id"] for t in active],
        "templates_dir": str(templates_dir()),
        "config": cfg,
    }


@app.post("/api/templates", tags=["Templates"])
def create_template_entry(
    payload: dict[str, Any] | None = Body(default=None),
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    """Create an empty template shell (then edit in the template editor)."""
    from templates.loader import create_template

    body = payload or {}
    oid = _request_owner_id(str(body.get("owner_id") or "") or None, x_braindance_owner_id)
    try:
        row = create_template(
            name=str(body.get("name") or "").strip() or None,
            project_id=str(body.get("project_id") or "").strip() or None,
            owner_id=oid,
        )
        return {"success": True, "template": row}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/templates/{template_id}", tags=["Templates"])
def get_template_entry(
    template_id: str,
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from templates.loader import assert_template_access, get_template

    row = get_template(template_id)
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        assert_template_access(row, _request_owner_id(None, x_braindance_owner_id))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return {"success": True, "template": row}


@app.patch("/api/templates/{template_id}", tags=["Templates"])
def patch_template_entry(
    template_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from templates.loader import assert_template_access, get_template, update_template

    row = get_template(template_id)
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    oid = _request_owner_id(None, x_braindance_owner_id)
    try:
        assert_template_access(row, oid)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    body = payload or {}
    try:
        caption_prompt_arg: str | None = None
        if "caption_prompt" in body or "custom_prompt" in body:
            caption_prompt_arg = str(
                body.get("caption_prompt")
                if "caption_prompt" in body
                else body.get("custom_prompt")
                or ""
            )
        row = update_template(
            template_id,
            name=body["name"] if "name" in body else None,
            project_id=body["project_id"] if "project_id" in body else None,
            active=body["active"] if "active" in body else None,
            caption_prompt=caption_prompt_arg,
            owner_id=body["owner_id"] if "owner_id" in body else None,
        )
        return {"success": True, "template": row}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/templates/{template_id}", tags=["Templates"])
def delete_template_entry(
    template_id: str,
    x_braindance_owner_id: Optional[str] = Header(default=None, alias="X-Braindance-Owner-Id"),
):
    from templates.loader import assert_template_access, delete_template, get_template

    row = get_template(template_id)
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        assert_template_access(row, _request_owner_id(None, x_braindance_owner_id))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    delete_template(template_id)
    return {"success": True}


@app.get("/api/templates/{template_id}/intro", tags=["Templates"])
def get_named_template_intro(template_id: str):
    """Stream the saved intro bumper for a template (if present)."""
    from templates.loader import get_template, template_dir, template_intro_path

    row = get_template(template_id)
    if not row:
        raise HTTPException(status_code=404, detail="Template not found")
    path = template_intro_path(template_id)
    if not path.is_file() or path.stat().st_size < 1000:
        # Hydrate bumper from Supabase Storage / DB when the local cache was wiped.
        try:
            from cloud_store import hydrate_template_to_disk

            hydrate_template_to_disk(template_id, template_dir(template_id))
        except Exception as exc:
            print(f"  [templates] intro hydrate failed: {exc}")
    if not path.is_file() or path.stat().st_size < 1000:
        raise HTTPException(status_code=404, detail="Template has no saved intro bumper")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{template_id}_intro.mp4",
        headers={"Cache-Control": "private, max-age=60"},
    )


@app.post("/api/templates/{template_id}/intro", tags=["Templates"])
async def upload_named_template_intro(
    template_id: str,
    video: UploadFile = File(...),
    project_id: Optional[str] = Form(default=None),
    name: Optional[str] = Form(default=None),
):
    """Save an Editor export as this template's intro bumper."""
    import tempfile

    from templates.loader import ensure_template, get_template, save_template_intro

    ensure_template(
        template_id,
        name=(name or "").strip() or None,
        project_id=(project_id or "").strip() or None,
    )

    tmp_path: Path | None = None
    bytes_written = 0
    try:
        with tempfile.NamedTemporaryFile(prefix="tpl_intro_", suffix=".mp4", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while chunk := await video.read(CHUNK_SIZE):
                tmp.write(chunk)
                bytes_written += len(chunk)
        if bytes_written < 1000:
            raise HTTPException(status_code=400, detail="Uploaded intro video is empty or too small.")
        row = save_template_intro(
            template_id,
            source_path=tmp_path,
            project_id=(project_id or "").strip() or None,
            name=(name or "").strip() or None,
        )
        row = get_template(template_id) or row
        return {
            "success": True,
            "template": row,
            "bytes": bytes_written,
            "editor_project_id": (row or {}).get("project_id"),
            "message": "Template saved.",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save template: {e}") from e
    finally:
        if tmp_path and tmp_path.is_file():
            try:
                tmp_path.unlink()
            except OSError:
                pass


@app.post("/api/templates/intro", tags=["Templates"])
async def upload_intro_template(
    video: UploadFile = File(...),
    project_id: Optional[str] = Form(default=None),
    template_id: Optional[str] = Form(default=None),
    name: Optional[str] = Form(default=None),
):
    """
    Back-compat upload. Prefer POST /api/templates/{id}/intro.
    Writes into the given template_id, or creates/uses the first/default entry.
    """
    from templates.loader import create_template, list_templates

    tid = (template_id or "").strip()
    if not tid:
        existing = list_templates()
        tid = existing[0]["id"] if existing else create_template(name=name or "Intro template")["id"]

    return await upload_named_template_intro(
        tid,
        video=video,
        project_id=project_id,
        name=name,
    )


@app.post("/api/media/to-mp3", tags=["Media"])
async def convert_media_to_mp3(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
):
    """
    Extract / convert uploaded media (mp4, wav, webm, …) to an MP3 download.
    Used by the Editor for timeline and per-clip audio export.
    """
    import tempfile

    from render.main import extract_audio

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    src_name = (file.filename or "input.bin").strip() or "input.bin"
    suffix = Path(src_name).suffix.lower() or ".bin"
    meta_title = (title or Path(src_name).stem or "clip").strip() or "clip"

    with tempfile.TemporaryDirectory(prefix="to_mp3_") as tmp:
        tmp_dir = Path(tmp)
        src = tmp_dir / f"input{suffix}"
        dst = tmp_dir / "output.mp3"
        src.write_bytes(raw)
        try:
            extract_audio(str(src), str(dst), audio_format="mp3", title=meta_title)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"MP3 conversion failed: {e}") from e
        if not dst.is_file():
            raise HTTPException(status_code=500, detail="MP3 conversion produced no file.")
        data = dst.read_bytes()

    safe_stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in meta_title)[:60].strip("_") or "clip"
    from fastapi.responses import Response

    return Response(
        content=data,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_stem}.mp3"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/connections", tags=["Connections"])
def list_connections(
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """
    Show which social platforms have credentials stored on this poster host.
    The background worker uses these to auto-post queued clips.
    Operator-only (YouTube-import allowlist).
    """
    from connections import get_connections_status

    return get_connections_status()


@app.post("/api/connections/{platform}/connect", tags=["Connections"])
def connect_platform(
    platform: str,
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """
    Start interactive login for youtube | instagram | tiktok on the poster host.
    Opens a browser on this machine; poll GET /api/connections until connected.
    """
    from connections import start_connect

    result = start_connect(platform)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Connect failed")
    return result


@app.delete("/api/connections/{platform}", tags=["Connections"])
@app.post("/api/connections/{platform}/disconnect", tags=["Connections"])
def disconnect_platform_route(
    platform: str,
    _user: dict[str, Any] = Depends(require_youtube_import_user),
):
    """Remove stored credentials for a platform on this poster host."""
    from connections import disconnect_platform

    result = disconnect_platform(platform)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Disconnect failed")
    return result


def main():
    """Entrypoint to run the server directly."""
    print(f"Starting Clip Poster Server on {HOST}:{PORT}...")
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
