#!/usr/bin/env python3
"""
worker.py — Queue worker for remote social posting.

Polls Supabase for clips that are due:
  - scheduled_at IS NULL  → post ASAP ("Post now")
  - scheduled_at <= now() → post when the scheduled time arrives

Checks local storage at:
  clips/[id]/clip.mp4

Caption / title / tags are read from the Supabase clip row.

Can run as a standalone CLI or embedded in the server as a background thread.

Usage:
    python worker.py
    python worker.py --once --dry-run
    python worker.py --poll-seconds 30
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# Ensure UTF-8 on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def get_clips_dir() -> Path:
    """Return the base directory where received clips are stored locally."""
    env_dir = os.environ.get("CLIPS_DIR", "clips").strip()
    p = Path(env_dir).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_clip_folder(clip_id: str | None) -> Path | None:
    """Return the folder path for a specific clip ID."""
    if not clip_id:
        return None
    return get_clips_dir() / str(clip_id).strip()


def _safe_clip_id(clip_id: str) -> str:
    clean = (clip_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", clean):
        raise ValueError(f"Invalid clip id: {clip_id!r}")
    return clean


def delete_clip_files(clip_id: str) -> dict[str, Any]:
    """Remove clips/{id}/ from local storage if it exists."""
    import shutil

    clean = _safe_clip_id(clip_id)
    folder = get_clips_dir() / clean
    if not folder.is_dir():
        return {
            "success": True,
            "id": clean,
            "deleted": False,
            "message": "No local files for this clip.",
        }
    shutil.rmtree(folder)
    return {
        "success": True,
        "id": clean,
        "deleted": True,
        "folder": str(folder),
    }


def load_clip_metadata(clip_id: str | None) -> dict[str, Any]:
    """
    Legacy helper — metadata.json is no longer written.
    Prefer caption/title/tags from the Supabase clip row.
    Still reads a leftover metadata.json if one exists.
    """
    folder = get_clip_folder(clip_id)
    if not folder or not folder.is_dir():
        return {}

    meta_file = folder / "metadata.json"
    if meta_file.is_file():
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"  [WARN] Failed to read {meta_file}: {e}")

    return {}


def resolve_video_path(storage_url: str | None, clip_id: str | None = None) -> str | None:
    """
    Resolve a clip's video file to a local file path this worker can upload.

    Checks:
      1. clips/[id]/clip.mp4 (primary server storage convention)
      2. clips/[id]/*.mp4 (any mp4 in clip folder)
      3. storage_url as an existing local path
      4. storage_url relative to CLIPS_DIR or CLIP_MEDIA_ROOT
      5. http(s) URLs (downloaded to a temp file)
    """
    # 1. Primary check: clips/[id]/clip.mp4
    folder = get_clip_folder(clip_id)
    if folder and folder.is_dir():
        primary = folder / "clip.mp4"
        if primary.is_file():
            return str(primary.resolve())
        # Fallback to any .mp4 file inside clips/[id]/
        mp4_files = sorted(folder.glob("*.mp4"))
        if mp4_files:
            return str(mp4_files[0].resolve())

    if not storage_url:
        return None

    url = storage_url.strip()
    if url.lower().startswith(("http://", "https://")):
        try:
            suffix = Path(url.split("?", 1)[0]).suffix or ".mp4"
            tmp = NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.close()
            print(f"  Downloading media → {tmp.name}")
            urllib.request.urlretrieve(url, tmp.name)
            return tmp.name
        except Exception as e:
            print(f"  [FAIL] Could not download storage_url: {e}")
            return None

    path = Path(url)
    if path.is_file():
        return str(path.resolve())

    clips_root = get_clips_dir()
    candidate_in_clips = clips_root / url
    if candidate_in_clips.is_file():
        return str(candidate_in_clips.resolve())

    media_root = os.environ.get("CLIP_MEDIA_ROOT", "").strip()
    if media_root:
        candidate = Path(media_root) / url
        if candidate.is_file():
            return str(candidate.resolve())
        # Also try basename under media root
        candidate2 = Path(media_root) / path.name
        if candidate2.is_file():
            return str(candidate2.resolve())

    return None


def _maybe_mark_clip_done(clip_id: str) -> None:
    """Mark clip posted only when every channel is in a terminal status."""
    from db import all_channels_terminal, mark_clip_posted, refresh_clip_channels

    fresh = refresh_clip_channels(clip_id)
    if not fresh:
        return
    if all_channels_terminal(fresh):
        mark_clip_posted(clip_id, True)
        print(f"  Clip {clip_id} fully terminal — marked posted.")
    else:
        print(f"  Clip {clip_id} still has open channels — leaving posted=false for retry.")


def process_clip(clip: dict, *, dry_run: bool, headless: bool, privacy: str) -> None:
    import time as _time

    from alerts import (
        EVENT_JOB_STUCK,
        EVENT_TEMP_FAILURE,
        EVENT_THREE_FAILURES,
        alert_for_outcome,
        emit,
    )
    from db import actionable_channels, claim_channel, update_channel
    from retry_state import (
        MAX_ATTEMPTS,
        begin_attempt,
        finish_attempt,
        get_attempts,
        is_stuck_processing,
    )
    from uploader import post_to_all

    clip_id = str(clip["id"])
    storage_url = clip.get("storage_url")
    scheduled = clip.get("scheduled_at") or "NOW (ASAP)"

    # Prefer Supabase metadata; fall back to legacy metadata.json if present
    legacy = load_clip_metadata(clip_id)
    caption = clip.get("caption") or legacy.get("caption") or ""
    title = clip.get("title") or legacy.get("title") or ""
    tags = clip.get("tags") or legacy.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    print("\n" + "=" * 60)
    print(f">> Clip {clip_id}")
    print(f"   title:        {title or '(none)'}")
    print(f"   caption:      {(caption[:60] + '...') if len(caption) > 60 else (caption or '(none)')}")
    print(f"   scheduled_at: {scheduled}")
    print(f"   storage_url:  {storage_url}")
    print("=" * 60)

    channels = actionable_channels(clip)
    if not channels:
        print("  No actionable channels.")
        _maybe_mark_clip_done(clip_id)
        return

    # Recover stuck processing rows before claiming
    still_actionable: list[dict] = []
    for ch in list(channels):
        if (ch.get("status") or "").lower() == "processing" and is_stuck_processing(
            str(ch["id"]), "processing"
        ):
            emit(
                EVENT_JOB_STUCK,
                detail=(
                    "Channel stuck in processing > threshold. "
                    "Marked uncertain — verify the account before any manual retry "
                    "(Post/Share may already have been clicked)."
                ),
                clip_id=clip_id,
                platform=str(ch.get("platform")),
            )
            # Conservative: do not auto-retry after a stuck in-flight upload
            update_channel(
                ch["id"],
                status="uncertain",
                error_message=(
                    "Reset after stuck processing — verify account before retrying. "
                    "Auto-retry disabled to prevent double-posting."
                ),
            )
            finish_attempt(str(ch["id"]), kind="uncertain", share_clicked=True)
            ch["status"] = "uncertain"
            continue
        still_actionable.append(ch)
    channels = still_actionable
    if not channels:
        print("  No actionable channels after stuck recovery.")
        _maybe_mark_clip_done(clip_id)
        return

    video_path = resolve_video_path(storage_url, clip_id=clip_id)
    if not video_path:
        err = (
            f"Video not found for clip {clip_id}. "
            f"Expected 'clips/{clip_id}/clip.mp4' or valid storage_url ({storage_url!r})."
        )
        print(f"  [FAIL] {err}")
        for ch in channels:
            cid = str(ch["id"])
            attempt = begin_attempt(cid)
            update_channel(cid, status="failed", error_message=err)
            finish_attempt(cid, kind="transient")
            if attempt >= MAX_ATTEMPTS:
                emit(
                    EVENT_THREE_FAILURES,
                    detail=err,
                    clip_id=clip_id,
                    platform=str(ch.get("platform")),
                )
            else:
                emit(
                    EVENT_TEMP_FAILURE,
                    detail=err,
                    clip_id=clip_id,
                    platform=str(ch.get("platform")),
                )
        _maybe_mark_clip_done(clip_id)
        return

    print(f"  Using video file: {video_path}")

    # Claim each channel before any upload (anti concurrent double-post)
    claimed: list[dict] = []
    for ch in channels:
        cid = str(ch["id"])
        prior = (ch.get("status") or "pending").lower()
        from_statuses = ["pending", "failed"] if prior != "processing" else ["processing", "pending", "failed"]
        if not claim_channel(cid, from_statuses=from_statuses):
            print(f"  [SKIP] Could not claim {ch.get('platform')} ({cid}) — already claimed or done.")
            continue
        begin_attempt(cid)
        claimed.append(ch)

    if not claimed:
        print("  Nothing claimed this cycle.")
        _maybe_mark_clip_done(clip_id)
        return

    platforms = [c["platform"] for c in claimed]
    platform_to_channel = {c["platform"]: c for c in claimed}

    results = post_to_all(
        video_path=video_path,
        title=title,
        caption=caption,
        tags=tags,
        platforms=platforms,
        dry_run=dry_run,
        privacy=privacy,
        move_to_posted=False,  # media lives in clips/[id]/
        headless=headless,
    )

    any_success = False
    for platform, outcome in results.items():
        ch = platform_to_channel.get(platform)
        if not ch:
            continue
        cid = str(ch["id"])
        attempt = int(get_attempts(cid) or 1)

        kind = alert_for_outcome(
            clip_id=clip_id,
            platform=platform,
            outcome=outcome or {},
            attempt=attempt,
            max_attempts=MAX_ATTEMPTS,
        )

        share_clicked = bool(
            (outcome or {}).get("share_clicked")
            or (outcome or {}).get("uncertain")
            or kind == "uncertain"
        )

        if kind == "success":
            any_success = True
            update_channel(cid, status="success", post_url=(outcome or {}).get("url"))
            finish_attempt(cid, kind="success")
        elif kind == "uncertain":
            update_channel(
                cid,
                status="uncertain",
                error_message=str(
                    (outcome or {}).get("error")
                    or "Publish unconfirmed — check account; auto-retry disabled."
                ),
                post_url=(outcome or {}).get("url"),
            )
            finish_attempt(cid, kind="uncertain", share_clicked=True)
        elif kind == "auth":
            update_channel(
                cid,
                status="failed",
                error_message=str((outcome or {}).get("error") or "Authentication failed"),
            )
            finish_attempt(cid, kind="auth")
        elif kind == "permanent":
            update_channel(
                cid,
                status="failed",
                error_message=str((outcome or {}).get("error") or "Permanent upload failure"),
            )
            finish_attempt(cid, kind="permanent")
        else:
            # transient — leave failed; retry_state schedules next_retry_at
            update_channel(
                cid,
                status="failed",
                error_message=str((outcome or {}).get("error") or "Upload failed"),
            )
            entry = finish_attempt(cid, kind="transient", share_clicked=share_clicked)
            if entry.get("next_retry_at"):
                wait = max(0, int(entry["next_retry_at"] - _time.time()))
                print(f"  [RETRY] {platform} will retry in ~{wait}s (attempt {attempt}/{MAX_ATTEMPTS})")

    # Any claimed platform missing from results (thread crash) → failed, no success lie
    for platform, ch in platform_to_channel.items():
        if platform in results:
            continue
        cid = str(ch["id"])
        err = "Uploader returned no outcome (worker thread error)."
        update_channel(cid, status="failed", error_message=err)
        finish_attempt(cid, kind="transient")
        alert_for_outcome(
            clip_id=clip_id,
            platform=platform,
            outcome={"success": False, "error": err},
            attempt=get_attempts(cid),
            max_attempts=MAX_ATTEMPTS,
        )

    _maybe_mark_clip_done(clip_id)
    print(f"  Clip {clip_id} cycle done · any_success={any_success}")


def _check_storage_usage(clips_root: Path) -> None:
    """Warn via Discord when CLIPS_DIR disk usage exceeds 85%."""
    from alerts import EVENT_STORAGE_HIGH, emit

    try:
        import shutil

        usage = shutil.disk_usage(str(clips_root))
        pct = (usage.used / usage.total) * 100 if usage.total else 0
        threshold = float(os.environ.get("STORAGE_WARN_PERCENT", "85"))
        if pct >= threshold:
            emit(
                EVENT_STORAGE_HIGH,
                detail=f"Disk at {pct:.1f}% used ({usage.used // (1024**3)}G / {usage.total // (1024**3)}G) on {clips_root}",
                dedupe_key=f"storage:{clips_root}",
            )
    except Exception as e:
        print(f"  [WARN] Storage check failed: {e}")


def run_loop(
    *,
    once: bool = False,
    poll_seconds: int = 30,
    dry_run: bool = False,
    headless: bool = True,
    privacy: str = "public",
    batch: int = 5,
    stop_event: threading.Event | None = None,
):
    from alerts import EVENT_SUPABASE_DOWN, emit
    from db import fetch_due_clips
    from retry_state import bump_supabase_fail, record_heartbeat, reset_supabase_fail

    clips_root = get_clips_dir()
    print("\n" + "=" * 60)
    print(">> Clip Poster Queue Worker")
    print(f"   poll every: {poll_seconds}s | dry_run={dry_run} | once={once}")
    print(f"   CLIPS_DIR:  {clips_root}")
    media = os.environ.get("CLIP_MEDIA_ROOT", "")
    if media:
        print(f"   CLIP_MEDIA_ROOT: {media}")
    print("=" * 60 + "\n")

    while True:
        if stop_event and stop_event.is_set():
            print("[Worker] Stop signal received. Exiting run loop.")
            break

        record_heartbeat()
        _check_storage_usage(clips_root)

        try:
            due = fetch_due_clips(limit=batch)
            reset_supabase_fail()
            if not due:
                print(f"[{time.strftime('%H:%M:%S')}] Queue empty — waiting {poll_seconds}s…")
            else:
                print(f"[{time.strftime('%H:%M:%S')}] Found {len(due)} due clip(s)")
                for clip in due:
                    if stop_event and stop_event.is_set():
                        break
                    try:
                        process_clip(clip, dry_run=dry_run, headless=headless, privacy=privacy)
                    except Exception as e:
                        print(f"  [FAIL] Unexpected error on clip {clip.get('id')}: {e}")
        except Exception as e:
            streak = bump_supabase_fail()
            print(f"[{time.strftime('%H:%M:%S')}] Poll error: {e}")
            # Critical after repeated Supabase/poll failures
            if streak >= int(os.environ.get("SUPABASE_FAIL_ALERT_AFTER", "3")):
                emit(
                    EVENT_SUPABASE_DOWN,
                    detail=f"Supabase/poll failed {streak} times in a row: {e}",
                    dedupe_key="supabase:unavailable",
                )

        if once:
            break

        wait_time = max(5, poll_seconds)
        if stop_event:
            if stop_event.wait(timeout=wait_time):
                print("[Worker] Stop signal received during wait.")
                break
        else:
            time.sleep(wait_time)


# Global background worker controls for server lifecycle
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


def is_worker_running() -> bool:
    global _worker_thread
    return _worker_thread is not None and _worker_thread.is_alive()


def start_background_worker(
    *,
    poll_seconds: int = 30,
    dry_run: bool = False,
    headless: bool = True,
    privacy: str = "public",
    batch: int = 5,
) -> threading.Thread:
    """Start the polling queue worker in a background daemon thread."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return _worker_thread

    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=run_loop,
        kwargs={
            "once": False,
            "poll_seconds": poll_seconds,
            "dry_run": dry_run,
            "headless": headless,
            "privacy": privacy,
            "batch": batch,
            "stop_event": _stop_event,
        },
        daemon=True,
        name="ClipPosterWorkerThread",
    )
    _worker_thread.start()
    return _worker_thread


def stop_background_worker(timeout: float = 5.0) -> None:
    """Signal the background worker to stop and wait for it."""
    global _worker_thread
    _stop_event.set()
    if _worker_thread is not None:
        _worker_thread.join(timeout=timeout)
        _worker_thread = None


def main():
    p = argparse.ArgumentParser(description="Clip poster queue worker (Supabase)")
    p.add_argument("--once", action="store_true", help="Process one poll cycle then exit")
    p.add_argument("--dry-run", action="store_true", help="Validate without live uploads")
    p.add_argument("--poll-seconds", type=int, default=int(os.environ.get("POLL_SECONDS", "30")))
    p.add_argument("--batch", type=int, default=5, help="Max due clips per cycle")
    p.add_argument("--privacy", default=os.environ.get("YOUTUBE_PRIVACY", "public"))
    p.add_argument("--headed", action="store_true", help="Show browser windows for IG/TikTok")
    args = p.parse_args()

    run_loop(
        once=args.once,
        poll_seconds=args.poll_seconds,
        dry_run=args.dry_run,
        headless=not args.headed,
        privacy=args.privacy,
        batch=args.batch,
    )


if __name__ == "__main__":
    main()
