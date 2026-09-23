"""
discovery/queue.py — Pull DJ-set URLs from Braindance and enqueue into discovery_runs.

Only inserts URLs that are not already present in discovery_runs (any status).
Does not apply the daily run cap — that only gates create_run when work starts.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_DJ_SET_FEED_URL = "https://braindance.hoochymoochy.xyz/dj-sets"


def get_dj_set_feed_url() -> str:
    return os.environ.get("DJ_SET_FEED_URL", DEFAULT_DJ_SET_FEED_URL).strip() or DEFAULT_DJ_SET_FEED_URL


def fetch_dj_sets(feed_url: str | None = None, *, timeout: float = 30.0) -> list[dict[str, Any]]:
    """GET the DJ-set feed and return a list of set objects."""
    url = feed_url or get_dj_set_feed_url()
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(f"DJ-set feed returned HTTP {e.code}: {body[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"DJ-set feed request failed: {e.reason}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"DJ-set feed returned invalid JSON: {e}") from e

    if isinstance(data, dict):
        # Allow common wrappers: {"sets": [...]} / {"data": [...]} / {"items": [...]}
        for key in ("sets", "data", "items", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise RuntimeError("DJ-set feed JSON must be a list (or a wrapped list).")

    if not isinstance(data, list):
        raise RuntimeError("DJ-set feed JSON must be a list of set objects.")

    return [item for item in data if isinstance(item, dict)]


def extract_urls(sets: list[dict[str, Any]]) -> list[str]:
    """Collect unique non-empty youtube URLs from set objects (order preserved)."""
    seen: set[str] = set()
    urls: list[str] = []
    for item in sets:
        url = (item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _existing_urls(supabase, urls: list[str]) -> set[str]:
    """Return the subset of urls that already exist in discovery_runs."""
    if not urls:
        return set()

    existing: set[str] = set()
    # PostgREST .in_ payloads stay manageable in chunks
    chunk_size = 100
    for i in range(0, len(urls), chunk_size):
        chunk = urls[i : i + chunk_size]
        result = (
            supabase.table("discovery_runs")
            .select("youtube_url")
            .in_("youtube_url", chunk)
            .execute()
        )
        for row in result.data or []:
            u = (row.get("youtube_url") or "").strip()
            if u:
                existing.add(u)
    return existing


def enqueue_urls(supabase, urls: list[str]) -> dict[str, Any]:
    """
    Insert pending discovery_runs rows for URLs not already in the table.

    Returns counts: fetched, inserted, skipped, inserted_urls.
    """
    existing = _existing_urls(supabase, urls)
    to_insert = [u for u in urls if u not in existing]
    inserted_rows: list[dict[str, Any]] = []

    for url in to_insert:
        result = (
            supabase.table("discovery_runs")
            .insert({"youtube_url": url, "status": "pending"})
            .execute()
        )
        if result.data:
            inserted_rows.append(result.data[0])

    return {
        "fetched": len(urls),
        "inserted": len(inserted_rows),
        "skipped": len(urls) - len(to_insert),
        "inserted_urls": [r["youtube_url"] for r in inserted_rows],
    }


def update_queue(supabase, feed_url: str | None = None) -> dict[str, Any]:
    """Fetch DJ-set feed and enqueue any new URLs into discovery_runs."""
    sets = fetch_dj_sets(feed_url)
    urls = extract_urls(sets)
    stats = enqueue_urls(supabase, urls)
    stats["feed_url"] = feed_url or get_dj_set_feed_url()
    return stats


def run_cron_cycle(
    supabase,
    *,
    feed_url: str | None = None,
    process: bool = True,
    max_runs: int = 1,
) -> dict[str, Any]:
    """
    One cron tick: refresh discovery_runs from Braindance, then process pending work.

    process=True runs the discovery pipeline (Ollama + GPU preview / approvals)
    up to max_runs claimed discovery_runs (plus any approved full-renders each cycle).
    """
    queue_stats = update_queue(supabase, feed_url=feed_url)
    result: dict[str, Any] = {
        "success": True,
        "queue": queue_stats,
        "processed": [],
    }
    if not process:
        return result

    from discovery.pipeline import process_once

    runs = max(0, int(max_runs))
    for _ in range(runs):
        outcome = process_once()
        result["processed"].append(outcome)
        # Stop early when nothing left to claim (still drains approvals once)
        if outcome.get("skipped"):
            break
        if not outcome.get("ok") and not outcome.get("candidates"):
            # Hard failure on a run — keep going only if more slots remain
            continue
    return result


def main() -> None:
    """CLI for cron: enqueue new URLs, then run the discovery worker once."""
    import argparse
    import sys
    from pathlib import Path

    # Allow `python queue.py` from discovery/ or repo root
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from dotenv import load_dotenv

    load_dotenv(root / ".env")

    parser = argparse.ArgumentParser(
        description="Cron entry: Braindance → discovery_runs → preview/render worker"
    )
    parser.add_argument(
        "--enqueue-only",
        action="store_true",
        help="Only refresh discovery_runs (skip GPU/Ollama processing)",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=int(os.environ.get("DISCOVERY_CRON_MAX_RUNS", "1") or "1"),
        help="Max discovery_runs to process after enqueue (default: 1)",
    )
    args = parser.parse_args()

    from db import get_supabase, is_configured

    if not is_configured():
        print("Supabase is not configured (set SUPABASE_URL and a key in .env).", file=sys.stderr)
        sys.exit(1)

    feed = get_dj_set_feed_url()
    print(f"Fetching {feed} ...")
    try:
        result = run_cron_cycle(
            get_supabase(),
            process=not args.enqueue_only,
            max_runs=args.max_runs,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    stats = result["queue"]
    print(
        f"fetched={stats['fetched']}  inserted={stats['inserted']}  skipped={stats['skipped']}"
    )
    for url in stats.get("inserted_urls") or []:
        print(f"  + {url}")

    if args.enqueue_only:
        return

    for i, outcome in enumerate(result.get("processed") or [], start=1):
        if outcome.get("skipped"):
            print(f"process[{i}]: skipped (no pending runs / daily cap)")
        elif outcome.get("ok"):
            n = len(outcome.get("candidates") or [])
            print(f"process[{i}]: ok candidates={n}")
        else:
            print(f"process[{i}]: failed — {outcome.get('error')}")
            sys.exit(1)


if __name__ == "__main__":
    main()
