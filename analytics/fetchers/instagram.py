#!/usr/bin/env python3
"""Instagram Reel / post stats via instagrapi (sessionid from poster host)."""

from __future__ import annotations

import time
from typing import Any

from .base import Metrics, extract_instagram_shortcode
from .retry import is_rate_limit_error, with_rate_limit_retry

# Reuse one logged-in client per process — login_by_sessionid on every post
# is what floods Instagram with 429s.
_client: Any = None
_client_sessionid: str | None = None


def reset_instagram_client() -> None:
    global _client, _client_sessionid
    _client = None
    _client_sessionid = None


def _get_client(sessionid: str):
    global _client, _client_sessionid
    if _client is not None and _client_sessionid == sessionid:
        return _client

    from instagrapi import Client

    def _login():
        cl = Client()
        # Mild delay between private API calls inside instagrapi.
        cl.delay_range = [1, 3]
        cl.login_by_sessionid(sessionid)
        return cl

    print("[instagram] Logging in with sessionid (once per worker run)…")
    cl = with_rate_limit_retry(
        "instagram",
        _login,
        label="login_by_sessionid",
    )
    _client = cl
    _client_sessionid = sessionid
    return cl


def _fetch_media(cl, shortcode: str):
    media_pk = cl.media_pk_from_code(shortcode)
    return cl.media_info(media_pk)


def fetch_instagram_metrics(post_url: str, *, sessionid: str | None = None) -> Metrics:
    shortcode = extract_instagram_shortcode(post_url)
    if not shortcode:
        return Metrics(error=f"Could not parse Instagram shortcode from URL: {post_url}")
    if not sessionid:
        return Metrics(
            error="No Instagram sessionid available "
            "(set INSTAGRAM_SESSIONID or clients.json authorization_data)"
        )

    try:
        from instagrapi import Client  # noqa: F401 — presence check
    except ImportError:
        return Metrics(error="instagrapi is not installed")

    try:
        cl = _get_client(sessionid)
        media = with_rate_limit_retry(
            "instagram",
            lambda: _fetch_media(cl, shortcode),
            label=f"media {shortcode}",
        )

        views = 0
        likes = int(getattr(media, "like_count", 0) or 0)
        comments = int(getattr(media, "comment_count", 0) or 0)
        for attr in ("play_count", "view_count", "video_view_count", "ig_play_count"):
            val = getattr(media, attr, None)
            if val is not None:
                try:
                    views = int(val)
                    break
                except (TypeError, ValueError):
                    pass

        raw = {
            "source": "instagrapi",
            "pk": str(getattr(media, "pk", "") or ""),
            "code": shortcode,
            "media_type": str(getattr(media, "media_type", "")),
        }
        # Small gap between posts so we don't burst Instagram.
        time.sleep(1.0)
        return Metrics(
            views=views,
            likes=likes,
            comments=comments,
            shares=0,
            saves=0,
            raw=raw,
        )
    except Exception as e:
        if is_rate_limit_error(e):
            # Drop cached client so the next clip can try a fresh login after cooldown.
            reset_instagram_client()
            print(f"[instagram] Giving up after retries — still rate limited: {e}")
            return Metrics(
                error=f"Instagram rate limited (429): {e}. "
                "Slow down pulls or wait a few minutes."
            )
        return Metrics(error=f"Instagram fetch error: {e}")
