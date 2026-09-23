"""Shared YouTube URL / timestamp helpers for discovery comment clustering."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")


def extract_youtube_video_id(url: str | None) -> str | None:
    if not url or not str(url).strip():
        return None
    raw = str(url).strip()

    lowered = raw.replace("\\", "/").lower()
    if lowered.endswith((".mp4", ".mkv", ".webm", ".mov")) and "youtu" not in lowered:
        return None
    if _YOUTUBE_ID_RE.match(raw):
        return raw

    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        if raw.startswith("//"):
            raw = "https:" + raw
        elif "youtu" in raw.lower():
            raw = "https://" + raw.lstrip("/")

    try:
        parsed = urllib.parse.urlparse(raw)
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path or ""
        qs = urllib.parse.parse_qs(parsed.query or "")

        youtube_hosts = (
            "youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtube-nocookie.com",
        )
        if host in youtube_hosts:
            if "v" in qs and qs["v"]:
                vid = (qs["v"][0] or "").strip()
                return vid or None
            m = re.match(r"^/(shorts|embed|live|v|watch)/([A-Za-z0-9_-]{6,})", path)
            if m:
                return m.group(2)
        if host == "youtu.be":
            vid = path.lstrip("/").split("/")[0]
            if vid:
                return vid
    except Exception:
        pass
    return None


def parse_timestamp_seconds(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    clock = re.fullmatch(
        r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?",
        text,
        re.I,
    )
    if clock and any(clock.groups()):
        hours = int(clock.group(1) or 0)
        minutes = int(clock.group(2) or 0)
        seconds = float(clock.group(3) or 0)
        return hours * 3600 + minutes * 60 + seconds

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)

    parts = text.replace(";", ":").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 1:
        return nums[0]
    return None


def normalize_clip_timestamp(value: Any) -> str | None:
    secs = parse_timestamp_seconds(value)
    if secs is None or secs < 0:
        return None
    total = int(round(secs))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def normalize_youtube_url(url: str | None) -> str:
    vid = extract_youtube_video_id(url)
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return (url or "").strip()
