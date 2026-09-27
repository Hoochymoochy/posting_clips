#!/usr/bin/env python3
"""
worker.py — Background analytics sync (same pipeline as GET /api/analytics/posts).

Usage (from posting_clips/):
  python -m analytics.worker
  python -m analytics.worker --once
  python -m analytics.worker --poll-seconds 1800
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from typing import Optional

from .config import get_settings
from .sync import pull_posts

_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_running = False


def is_worker_running() -> bool:
    return _running


def run_once(*, platform: str | None = None) -> dict:
    print("[analytics-worker] Pulling analytics for all published posts...")
    posts = pull_posts(platform=platform)
    channels = sum(len(p.get("channels") or []) for p in posts)
    errors = sum(
        1
        for p in posts
        for ch in (p.get("channels") or [])
        if ch.get("error")
    )
    summary = {
        "clips": len(posts),
        "channels": channels,
        "errors": errors,
    }
    print(
        f"[analytics-worker] Done: {summary['clips']} clips, "
        f"{summary['channels']} channels, {summary['errors']} errors"
    )
    return summary


def run_loop(*, poll_seconds: int = 3600) -> None:
    global _running
    _running = True
    _stop_event.clear()
    print(f"[analytics-worker] Loop started (interval={poll_seconds}s)")
    try:
        while not _stop_event.is_set():
            try:
                run_once()
            except Exception:
                traceback.print_exc()
            deadline = time.time() + max(poll_seconds, 5)
            while time.time() < deadline:
                if _stop_event.is_set():
                    break
                time.sleep(1)
    finally:
        _running = False
        print("[analytics-worker] Loop stopped")


def start_background_worker(*, poll_seconds: int = 3600) -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=run_loop,
        kwargs={"poll_seconds": poll_seconds},
        name="analytics-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_background_worker() -> None:
    _stop_event.set()
    t = _worker_thread
    if t and t.is_alive():
        t.join(timeout=5)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Analytics sync worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle and exit",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=settings["poll_interval_seconds"],
        help="Seconds between sync cycles (default from ANALYTICS_POLL_SECONDS)",
    )
    parser.add_argument(
        "--platform",
        choices=["youtube", "instagram"],
        default=None,
        help="Only sync one platform (TikTok currently disabled)",
    )
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.once:
        run_once(platform=args.platform)
        return 0

    try:
        run_loop(poll_seconds=args.poll_seconds)
    except KeyboardInterrupt:
        print("\n[analytics-worker] Interrupted")
        stop_background_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
