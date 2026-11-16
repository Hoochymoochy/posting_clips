"""
Hard content filters for discovery ingestion.

Blocked sets are dropped before enqueue so they never consume GPU/Ollama.
"""

from __future__ import annotations

import re
from typing import Any

# Case-insensitive whole-word (or hyphenated) matches in title / channel / genres.
HARD_BLOCK_KEYWORDS: tuple[str, ...] = ("bollywood",)

_HARD_BLOCK_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(" + "|".join(re.escape(k) for k in HARD_BLOCK_KEYWORDS) + r")(?:[^a-z0-9]|$)"
)


def _haystacks(item: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for key in ("title", "channel"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    genres = item.get("genres")
    if isinstance(genres, list):
        parts.extend(str(g) for g in genres if g)
    elif isinstance(genres, str) and genres.strip():
        parts.append(genres)
    return parts


def hard_block_reason(
    item: dict[str, Any] | None = None,
    *,
    text: str | None = None,
) -> str | None:
    """
    Return the matched hard-block keyword, or None if the item is allowed.

    Checks feed fields (title, channel, genres) and/or an explicit text blob
    (e.g. YouTube video_title from the pipeline).
    """
    blobs = list(_haystacks(item or {}))
    if text and str(text).strip():
        blobs.append(str(text))
    for blob in blobs:
        match = _HARD_BLOCK_RE.search(blob)
        if match:
            return match.group(1).lower()
    return None


def is_hard_blocked(item: dict[str, Any] | None = None, *, text: str | None = None) -> bool:
    return hard_block_reason(item, text=text) is not None


def filter_hard_blocked_sets(
    sets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """
    Drop hard-blocked sets.

    Returns (kept, dropped_count, matched_keywords_for_dropped).
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    reasons: list[str] = []
    for item in sets:
        reason = hard_block_reason(item)
        if reason is not None:
            dropped += 1
            reasons.append(reason)
            continue
        kept.append(item)
    return kept, dropped, reasons
