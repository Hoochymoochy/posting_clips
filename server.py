#!/usr/bin/env python3
"""
server.py — FastAPI backend server for clip uploads and background posting worker.

Features:
  1. Video ingestion API:
     - Stores received videos in: clips/[id]/clip.mp4
     - Caption / title / tags live in Supabase (not metadata.json)
  2. Background polling service:
     - Starts automatically in the background (unless disabled via ENABLE_BACKGROUND_WORKER=false)
     - Queries Supabase for due clips
     - Uploads to YouTube Shorts, Instagram Reels, and TikTok
  3. Status & Health endpoints:
     - GET /health
      - GET /api/clips
      - GET /api/clips/{id}
      - DELETE /api/clips/{id}
     - GET /api/connections
     - POST /api/connections/{platform}/connect
     - POST /api/connections/{platform}/disconnect
     - POST /api/worker/poll-now
     - POST /api/update-queue
"""

from __future__ import annotations

import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from worker import (
    get_clips_dir,
    is_worker_running,
    run_loop,
    start_background_worker,
    stop_background_worker,
)

load_dotenv()

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
ENABLE_WORKER = os.environ.get("ENABLE_BACKGROUND_WORKER", "true").lower() in ("1", "true", "yes")


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

    yield

    # Shutdown: Stop background worker
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
    """Check storage status for a specific clip ID."""
    clips_dir = get_clips_dir()
    folder = clips_dir / id.strip()

    if not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"Clip {id} not found in {clips_dir}.")

    video_file = folder / "clip.mp4"
    has_video = video_file.is_file()
    video_size = video_file.stat().st_size if has_video else 0

    return {
        "id": id,
        "folder": str(folder),
        "video_exists": has_video,
        "video_path": str(video_file) if has_video else None,
        "video_size_bytes": video_size,
        "storage_url": f"clips/{id}/clip.mp4" if has_video else None,
    }


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
def update_discovery_queue():
    """
    Fetch DJ sets from Braindance and enqueue new youtube URLs into discovery_runs.
    URLs already present in discovery_runs are skipped.
    """
    from db import get_supabase, is_configured
    from discovery.queue import update_queue

    if not is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured.")

    try:
        stats = update_queue(get_supabase())
        return {"success": True, **stats}
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/connections", tags=["Connections"])
def list_connections():
    """
    Show which social platforms have credentials stored on this poster host.
    The background worker uses these to auto-post queued clips.
    """
    from connections import get_connections_status

    return get_connections_status()


@app.post("/api/connections/{platform}/connect", tags=["Connections"])
def connect_platform(platform: str):
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
def disconnect_platform_route(platform: str):
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
