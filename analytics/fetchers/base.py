#!/usr/bin/env python3
"""Shared metrics types and post URL parsers."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Metrics:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    watch_time_seconds: float = 0.0
    average_view_duration_seconds: float | None = None
    average_view_percentage: float | None = None
    retention_curve: list[dict[str, float]] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["raw_metrics"] = d.pop("raw")
        return d


def extract_youtube_video_id(url: str | None) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    try:
        parsed = urllib.parse.urlparse(raw if "://" in raw else f"https://{raw}")
    except Exception:
        return None
    host = (parsed.netloc or "").lower().replace("www.", "")
    path = parsed.path or ""
    qs = urllib.parse.parse_qs(parsed.query or "")

    if host in ("youtu.be",):
        vid = path.strip("/").split("/")[0]
        return vid or None
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if path.startswith("/shorts/"):
            return path.split("/shorts/")[1].split("/")[0] or None
        if path.startswith("/embed/") or path.startswith("/v/"):
            return path.strip("/").split("/")[1] if "/" in path.strip("/") else None
        if "v" in qs and qs["v"]:
            return qs["v"][0]
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "watch":
            return parts[1]
    return None


def extract_instagram_shortcode(url: str | None) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    m = re.search(r"instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_-]+)", raw, re.I)
    if m:
        return m.group(1)
    return None


def extract_tiktok_video_id(url: str | None) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    low = raw.lower()
    if "tiktokstudio" in low or low.rstrip("/").endswith("tiktok.com"):
        return None
    m = re.search(r"/video/(\d+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"tiktok\.com/t/([A-Za-z0-9]+)", raw, re.I)
    if m:
        return m.group(1)
    return None


def is_usable_post_url(platform: str, url: str | None) -> bool:
    if not url or not str(url).strip():
        return False
    p = (platform or "").lower().strip()
    if p == "youtube":
        return bool(extract_youtube_video_id(url))
    if p == "instagram":
        return bool(extract_instagram_shortcode(url))
    if p == "tiktok":
        return bool(extract_tiktok_video_id(url)) or (
            "tiktok.com" in url.lower() and "tiktokstudio" not in url.lower()
        )
    return False
