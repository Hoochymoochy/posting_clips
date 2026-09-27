#!/usr/bin/env python3
"""TikTok video stats via yt-dlp when a real video URL is available."""

from __future__ import annotations

from typing import Any

from .base import Metrics, extract_tiktok_video_id, is_usable_post_url


def fetch_tiktok_metrics(post_url: str) -> Metrics:
    if not is_usable_post_url("tiktok", post_url):
        return Metrics(
            error=(
                "TikTok post_url is not a video link "
                "(uploader may have stored a Studio fallback URL)"
            )
        )

    video_id = extract_tiktok_video_id(post_url)
    try:
        import yt_dlp
    except ImportError:
        return Metrics(error="yt-dlp is not installed")

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(post_url, download=False)
        if not info:
            return Metrics(error="yt-dlp returned no TikTok info")

        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0] or info

        views = int(info.get("view_count") or info.get("play_count") or 0)
        likes = int(info.get("like_count") or info.get("repost_count") or 0)
        if info.get("like_count") is not None:
            likes = int(info.get("like_count") or 0)
        comments = int(info.get("comment_count") or 0)
        shares = int(info.get("repost_count") or info.get("share_count") or 0)

        return Metrics(
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            saves=0,
            raw={
                "source": "yt-dlp",
                "id": info.get("id") or video_id,
                "title": info.get("title"),
                "uploader": info.get("uploader"),
            },
        )
    except Exception as e:
        return Metrics(error=f"yt-dlp TikTok error: {e}")
