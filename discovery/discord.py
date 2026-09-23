#!/usr/bin/env python3
"""Thin Discord webhook helper for discovery review notifications."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


def _webhook_url() -> str:
    return (
        os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        or os.environ.get("DISCORD_BOT_WEBHOOK", "").strip()
    )


def send_discord(
    content: str,
    *,
    title: str | None = None,
    fields: list[dict[str, Any]] | None = None,
) -> bool:
    """Post a simple embed to Discord. Returns True if sent."""
    url = _webhook_url()
    if not url:
        print(f"  [DISCORD] (no DISCORD_WEBHOOK_URL) {title or content}")
        return False

    embed: dict[str, Any] = {
        "title": title or "Discovery",
        "description": (content or "")[:4000],
        "color": 0x5865F2,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = [
            {
                "name": str(f.get("name", ""))[:256],
                "value": str(f.get("value", ""))[:1024],
                "inline": bool(f.get("inline", False)),
            }
            for f in fields
            if f.get("name")
        ]

    payload = {
        "username": os.environ.get("DISCORD_BOT_NAME", "Clip Discovery"),
        "embeds": [embed],
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "dj-clipping-discovery/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
            if ok:
                print(f"  [DISCORD] Sent: {title or content[:60]}")
            return ok
    except urllib.error.HTTPError as e:
        print(f"  [WARN] Discord webhook HTTP {e.code}: {e.read()[:200]!r}")
        return False
    except Exception as e:
        print(f"  [WARN] Discord webhook failed: {e}")
        return False


def notify_review_ready(*, count: int, review_url: str, youtube_url: str = "") -> bool:
    """Notify that discovery previews are ready for mobile review."""
    fields = [
        {"name": "Clips ready", "value": str(count), "inline": True},
        {"name": "Review", "value": review_url, "inline": False},
    ]
    if youtube_url:
        fields.insert(0, {"name": "Source", "value": youtube_url[:1024], "inline": False})
    return send_discord(
        f"{count} clip preview{'s' if count != 1 else ''} ready — open the link on your phone and swipe to approve or decline.",
        title="Review clips",
        fields=fields,
    )
