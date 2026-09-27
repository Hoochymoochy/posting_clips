#!/usr/bin/env python3
"""Orchestrate live analytics pulls and persist snapshots."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db
from .config import ANALYTICS_PLATFORMS, get_instagram_sessionid, get_settings
from .fetchers.base import Metrics, is_usable_post_url
from .fetchers.instagram import fetch_instagram_metrics, reset_instagram_client
from .fetchers.retry import is_rate_limit_error
from .fetchers.youtube import fetch_youtube_metrics


def fetch_metrics_for_channel(channel: dict[str, Any]) -> Metrics:
    platform = (channel.get("platform") or "").lower().strip()
    post_url = (channel.get("post_url") or "").strip()
    settings = get_settings()

    if platform not in ANALYTICS_PLATFORMS:
        return Metrics(error=f"Platform skipped (not enabled): {platform}")

    if not is_usable_post_url(platform, post_url):
        return Metrics(
            error=f"Unusable or missing post_url for {platform}: {post_url or '(empty)'}"
        )

    print(f"[{platform}] Fetching metrics for {post_url}")

    if platform == "youtube":
        return fetch_youtube_metrics(
            post_url, api_key=settings.get("youtube_api_key") or None
        )
    if platform == "instagram":
        return fetch_instagram_metrics(post_url, sessionid=get_instagram_sessionid())
    return Metrics(error=f"Unsupported platform: {platform}")


def _channel_response(
    channel: dict[str, Any],
    metrics: Metrics,
    captured_at: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "channel_id": channel.get("id"),
        "platform": channel.get("platform"),
        "post_url": channel.get("post_url"),
        "views": metrics.views,
        "likes": metrics.likes,
        "comments": metrics.comments,
        "shares": metrics.shares,
        "saves": metrics.saves,
        # Primary retention signal: % of video length watched on average.
        "average_watch_percent": metrics.average_view_percentage,
        "average_view_duration_seconds": metrics.average_view_duration_seconds,
        "captured_at": captured_at,
        "error": metrics.error,
    }
    if metrics.retention_curve:
        row["retention_curve"] = metrics.retention_curve
    retention = (metrics.raw or {}).get("retention")
    if isinstance(retention, dict) and retention.get("error"):
        row["retention_error"] = retention["error"]
    return row


def pull_channel_analytics(channel: dict[str, Any]) -> dict[str, Any]:
    """Fetch one channel, write snapshot + denormalized columns, return response row."""
    clip_rel = channel.get("clips") or {}
    clip_id = channel.get("clip_id") or clip_rel.get("id")
    if not clip_id:
        raise ValueError("channel missing clip_id")

    metrics = fetch_metrics_for_channel(channel)
    captured_at = datetime.now(timezone.utc).isoformat()

    snapshot = db.insert_analytics_snapshot(
        clip_id=clip_id,
        channel_id=channel.get("id"),
        platform=(channel.get("platform") or "").lower(),
        post_url=channel.get("post_url"),
        views=metrics.views,
        likes=metrics.likes,
        comments=metrics.comments,
        shares=metrics.shares,
        saves=metrics.saves,
        watch_time_seconds=metrics.watch_time_seconds,
        raw_metrics=metrics.raw,
        error=metrics.error,
    )
    if snapshot.get("captured_at"):
        captured_at = snapshot["captured_at"]

    if not metrics.error and channel.get("id"):
        db.update_channel_metrics(
            channel["id"],
            views=metrics.views,
            likes=metrics.likes,
            comments=metrics.comments,
            shares=metrics.shares,
            saves=metrics.saves,
        )

    return _channel_response(channel, metrics, captured_at)


def _group_by_clip(
    channels: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ch in channels:
        clip_rel = ch.get("clips") or {}
        clip_id = ch.get("clip_id") or clip_rel.get("id")
        if clip_id:
            grouped[str(clip_id)].append(ch)
    return grouped


def _clip_title(channel: dict[str, Any]) -> str | None:
    clip_rel = channel.get("clips") or {}
    return clip_rel.get("title")


def pull_posts(
    *,
    clip_id: str | None = None,
    platform: str | None = None,
) -> list[dict[str, Any]]:
    """
    Live-pull analytics for published channels and return clip-grouped payloads.
    """
    # Fresh Instagram session each full sync — avoids stale throttled clients.
    reset_instagram_client()

    channels = db.list_published_channels(clip_id=clip_id, platform=platform)
    if not channels:
        return []

    grouped = _group_by_clip(channels)
    results: list[dict[str, Any]] = []

    for cid, ch_list in grouped.items():
        channel_payloads: list[dict[str, Any]] = []
        title = _clip_title(ch_list[0]) if ch_list else None
        for ch in ch_list:
            plat = (ch.get("platform") or "?").lower()
            try:
                row = pull_channel_analytics(ch)
                channel_payloads.append(row)
                if row.get("error") and is_rate_limit_error(row["error"]):
                    print(
                        f"[{plat}] Rate limit hit — pausing 3s before next channel…"
                    )
                    time.sleep(3)
            except Exception as e:
                err = str(e)
                if is_rate_limit_error(e):
                    print(f"[{plat}] Unhandled rate limit: {err}")
                    err = f"{plat} rate limited (429): {err}"
                    time.sleep(3)
                channel_payloads.append(
                    {
                        "channel_id": ch.get("id"),
                        "platform": ch.get("platform"),
                        "post_url": ch.get("post_url"),
                        "views": 0,
                        "likes": 0,
                        "comments": 0,
                        "shares": 0,
                        "saves": 0,
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "error": err,
                    }
                )

        try:
            db.rollup_clip_totals(cid)
        except Exception:
            pass

        results.append(
            {
                "clip_id": cid,
                "title": title,
                "channels": channel_payloads,
            }
        )

    if get_settings().get("auto_export_json"):
        _maybe_export(results)

    return results


def _maybe_export(results: list[dict[str, Any]]) -> None:
    try:
        settings = get_settings()
        exports: Path = settings["exports_dir"]
        exports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = exports / f"analytics_{stamp}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"captured_at": stamp, "posts": results},
                f,
                indent=2,
            )
    except Exception as e:
        print(f"[export] Failed to write JSON export: {e}")
