"""
discovery/pipeline.py — Claim discovery_runs → cluster → Ollama → GPU preview.

Also drains approved candidates (full render + queue) on this backend host.

Runs as a background thread from FastAPI, or:
  python -m discovery.pipeline --once
  python -m discovery.pipeline --poll-seconds 60
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# Allow `python -m discovery.pipeline` from posting_clips/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _review_base_url() -> str:
    return (
        os.environ.get("REVIEW_BASE_URL", "").strip().rstrip("/")
        or os.environ.get("PUBLIC_WEB_BASE_URL", "").strip().rstrip("/")
        or "http://127.0.0.1:5173"
    )


def _api_public_base() -> str:
    return (
        os.environ.get("PUBLIC_API_BASE_URL", "").strip().rstrip("/")
        or os.environ.get("CLIP_API_BASE_URL", "").strip().rstrip("/")
        or ""
    )


def _safe_stem(text: str, *, fallback: str = "clip") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (text or "").strip())[:40].strip("_")
    return cleaned or fallback


def _caption_for_entry(entry: dict[str, Any], video_title: str) -> str:
    from ollama_caption import rewrite_caption_with_ollama

    primary = (entry.get("text") or "").strip()
    extras = [
        str(c.get("text") or "").strip()
        for c in (entry.get("comments") or [])
        if isinstance(c, dict) and str(c.get("text") or "").strip()
    ]
    if primary and primary not in extras:
        extras = [primary, *extras]
    result = rewrite_caption_with_ollama(
        primary or video_title,
        video_title=video_title,
        entry_timestamp=str(entry.get("entry_timestamp") or ""),
        extra_comments=extras[1:] if len(extras) > 1 else extras,
    )
    return (result.get("caption") or primary or video_title or "New DJ clip").strip()


def process_run(run: dict[str, Any]) -> dict[str, Any]:
    from discovery.comment_entries import (
        DISCOVERY_PREVIEW_COUNT,
        find_clustered_comment_entries,
    )
    from discovery.candidates import (
        discovery_preview_path,
        insert_candidate,
        public_candidate,
        update_candidate,
    )
    from discovery.discord import notify_review_ready
    from discovery.limiter import mark_run_failed, mark_run_success
    from discovery.paths import workspace_dir
    from discovery.render import render_preview
    from db import get_supabase

    run_id = run["id"]
    youtube_url = (run.get("youtube_url") or "").strip()
    if not youtube_url:
        mark_run_failed(get_supabase(), run_id)
        return {"ok": False, "error": "missing youtube_url", "candidates": []}

    print(f"[discovery] processing run {run_id}: {youtube_url}")

    clustered = find_clustered_comment_entries(
        youtube_url,
        top_n=DISCOVERY_PREVIEW_COUNT,
    )
    if not clustered.get("success"):
        mark_run_failed(get_supabase(), run_id)
        return {"ok": False, "error": clustered.get("error") or "comment scrape failed", "candidates": []}

    entries = list(clustered.get("entries") or [])
    video_title = (clustered.get("video_title") or "").strip()
    if not entries:
        mark_run_failed(get_supabase(), run_id)
        return {
            "ok": False,
            "error": clustered.get("message") or "No timestamped comments",
            "candidates": [],
        }

    segments_dir = workspace_dir() / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    sb = get_supabase()
    ready: list[dict[str, Any]] = []
    errors: list[str] = []
    api_base = _api_public_base()

    for entry in entries:
        start = entry.get("entry_timestamp") or "00:00:00"
        end = entry.get("end_timestamp") or start
        caption = _caption_for_entry(entry, video_title)
        stem = f"{_safe_stem(video_title)}_{_safe_stem(start, fallback='t')}"
        segment_path = segments_dir / f"{stem}_{uuid.uuid4().hex[:8]}_segment.mp4"

        row = insert_candidate(
            sb,
            youtube_url=youtube_url,
            discovery_run_id=run_id,
            start_time=start,
            end_time=end,
            title=video_title or None,
            caption=caption,
            source_comments=entry.get("comments") or [
                {"text": entry.get("text"), "like_count": entry.get("like_count")}
            ],
            status="previewing",
        )
        candidate_id = row["id"]
        preview_path = discovery_preview_path(candidate_id)

        try:
            render_preview(
                youtube_url=youtube_url,
                start=start,
                end=end,
                title=video_title or "DJ Clip",
                out_path=preview_path,
                segment_path=segment_path,
            )
            preview_url = (
                f"{api_base}/api/review/{candidate_id}/preview"
                if api_base
                else f"/api/review/{candidate_id}/preview"
            )
            updated = update_candidate(
                sb,
                candidate_id,
                status="awaiting_review",
                preview_path=str(preview_path),
                preview_url=preview_url,
                segment_path=str(segment_path) if segment_path.is_file() else None,
            )
            ready.append(public_candidate(updated, api_base=api_base))
            print(f"  [ok] candidate {candidate_id} awaiting_review")
        except Exception as exc:
            msg = str(exc)[:2000]
            errors.append(msg)
            update_candidate(sb, candidate_id, status="failed", error_message=msg)
            print(f"  [fail] candidate {candidate_id}: {msg[:200]}")

    if ready:
        mark_run_success(sb, run_id)
        review_url = _review_base_url().rstrip("/")
        if not review_url.endswith("/review"):
            # SPA lives at / — Discord can open root; keep /review path if used
            review_url = f"{review_url}/"
        notify_review_ready(count=len(ready), review_url=review_url, youtube_url=youtube_url)
        return {"ok": True, "candidates": ready, "errors": errors}

    mark_run_failed(sb, run_id)
    return {"ok": False, "error": "; ".join(errors) or "all previews failed", "candidates": []}


def process_approvals() -> int:
    from discovery.candidates import list_candidates
    from discovery.review import full_render_and_queue
    from db import get_supabase

    sb = get_supabase()
    rows = list_candidates(sb, status="approved", limit=5)
    done = 0
    for row in rows:
        cid = row["id"]
        try:
            full_render_and_queue(sb, cid)
            print(f"  [approve] full render queued for {cid}")
            done += 1
        except Exception as exc:
            print(f"  [WARN] approve render failed for {cid}: {exc}")
    return done


def process_once() -> dict[str, Any]:
    from discovery.limiter import claim_next_pending_run
    from db import get_supabase, is_configured

    if not is_configured():
        raise RuntimeError("Supabase is not configured (SUPABASE_URL / key in .env).")

    try:
        n = process_approvals()
        if n:
            print(f"  [cycle] approvals={n}")
    except Exception as exc:
        print(f"  [WARN] approval cycle failed: {exc}")

    sb = get_supabase()
    run = claim_next_pending_run(sb)
    if not run:
        print("No pending discovery runs (or daily cap reached).")
        return {"ok": True, "skipped": True, "candidates": []}
    return process_run(run)


# --- Background thread (FastAPI lifespan) ---

_discovery_thread: threading.Thread | None = None
_discovery_stop = threading.Event()


def is_discovery_worker_running() -> bool:
    return _discovery_thread is not None and _discovery_thread.is_alive()


def start_discovery_worker(*, poll_seconds: int | None = None) -> threading.Thread:
    global _discovery_thread
    if _discovery_thread is not None and _discovery_thread.is_alive():
        return _discovery_thread

    seconds = poll_seconds if poll_seconds is not None else _env_int("DISCOVERY_POLL_SECONDS", 120)
    _discovery_stop.clear()

    def _loop() -> None:
        print(f"[Discovery] Worker polling every {seconds}s …")
        while not _discovery_stop.is_set():
            try:
                process_once()
            except Exception as exc:
                print(f"[Discovery] cycle error: {exc}")
            _discovery_stop.wait(timeout=max(5, int(seconds)))

    _discovery_thread = threading.Thread(
        target=_loop,
        daemon=True,
        name="DiscoveryPipelineThread",
    )
    _discovery_thread.start()
    return _discovery_thread


def stop_discovery_worker(timeout: float = 5.0) -> None:
    global _discovery_thread
    _discovery_stop.set()
    if _discovery_thread is not None:
        _discovery_thread.join(timeout=timeout)
        _discovery_thread = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Discovery GPU/Ollama pipeline (backend)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=_env_int("DISCOVERY_POLL_SECONDS", 120),
    )
    args = parser.parse_args()

    if args.once:
        result = process_once()
        if not result.get("ok") and not result.get("skipped"):
            print(f"Failed: {result.get('error')}", file=sys.stderr)
            sys.exit(1)
        return

    start_discovery_worker(poll_seconds=args.poll_seconds)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_discovery_worker()


if __name__ == "__main__":
    main()
