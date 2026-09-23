#!/usr/bin/env python3
"""Find high-engagement YouTube comment timestamps as clip entry points."""

from __future__ import annotations

import os
import re
import shutil
from typing import Any

from discovery.youtube_meta import (
    extract_youtube_video_id,
    normalize_clip_timestamp,
    parse_timestamp_seconds,
)

# Default clip window from a comment timestamp (seconds).
DEFAULT_ENTRY_DURATION_SEC = 30
DEFAULT_TOP_N = 5
DEFAULT_MAX_COMMENTS = 400
# Merge timestamped comments within this gap into one discovery clip window.
DEFAULT_CLUSTER_GAP_SEC = 45
# YouTube Shorts hard cap.
MAX_CLIP_DURATION_SEC = 60
DISCOVERY_PREVIEW_COUNT = 2

# Timestamps that look like clip markers inside free-form comment text.
# Prefer longer matches (H:MM:SS) before MM:SS / M:SS.
_TIMESTAMP_IN_TEXT_RE = re.compile(
    r"(?<![\d.])"
    r"(?:"
    r"\d{1,2}:\d{2}:\d{2}"  # 1:23:45
    r"|"
    r"\d{1,3}:\d{2}"  # 41:13 or 1:23
    r")"
    r"(?![\d.])"
)

_TRACKLIST_WORD_RE = re.compile(
    r"(?i)(?:^|\b)(track\s*lists?|set\s*lists?|timestamps?)\b\s*:?",
)
# Typical tracklist line: "41:10 Artist – Song" / "1:18 w/ Artist – Song"
_TRACKLIST_LINE_RE = re.compile(
    r"(?:^|[\n\r])\s*(?:\d{1,2}:)?\d{1,3}:\d{2}\.?\s+(?:w/\s+)?[^\n\r]{3,}",
    re.I,
)
# Pipe tracklists: "00:00 | Turnstile - NEVER ENOUGH"
_PIPE_TRACK_ROW_RE = re.compile(
    r"(?<![\d.])(?:\d{1,2}:)?\d{1,3}:\d{2}(?![\d.])\s*\|\s*\S+",
)


def _parse_like_count(raw: Any) -> int:
    """Parse yt-dlp / YouTube like fields (int, float, '1,234', '1.2K', '3M')."""
    if raw is None or raw is False:
        return 0
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    text = str(raw).strip().replace(",", "").upper()
    if not text:
        return 0
    mult = 1
    if text.endswith("K"):
        mult = 1_000
        text = text[:-1]
    elif text.endswith("M"):
        mult = 1_000_000
        text = text[:-1]
    elif text.endswith("B"):
        mult = 1_000_000_000
        text = text[:-1]
    try:
        return max(0, int(float(text) * mult))
    except (TypeError, ValueError):
        return 0


def _comment_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize yt-dlp / extractor comment dict shapes."""
    text = (
        raw.get("text")
        or raw.get("content")
        or raw.get("comment")
        or ""
    )
    if not isinstance(text, str):
        text = str(text or "")

    like_count = 0
    for key in (
        "like_count",
        "likes",
        "likeCount",
        "vote_count",
        "voteCount",
        "votes",
    ):
        if key not in raw or raw.get(key) is None:
            continue
        like_count = _parse_like_count(raw.get(key))
        break

    author = raw.get("author") or raw.get("author_name") or raw.get("user") or None
    if author is not None:
        author = str(author).strip() or None

    comment_id = raw.get("id") or raw.get("comment_id") or ""
    return {
        "text": text.strip(),
        "like_count": max(0, like_count),
        "likes": max(0, like_count),  # alias for UI
        "author": author,
        "comment_id": str(comment_id) if comment_id else "",
    }


def count_timestamps_in_comment(text: str) -> int:
    if not text:
        return 0
    return len(_TIMESTAMP_IN_TEXT_RE.findall(text))


def is_tracklist_comment(text: str) -> bool:
    """
    True for pasted set tracklists (many timestamps / song rows), not moment reactions.

    Junk examples:
      - "TRACKLIST: 0:01. ID – ID 1:18 w/ Calvin Harris – Blame ..."
      - "00:00 | Turnstile - NEVER ENOUGH\\n03:00 | KETTAMA - Raw Cuts ..."
    Keep: "41:13 the most underrated calvin song!"
    """
    raw = (text or "").strip()
    if not raw:
        return True

    # Explicit tracklist / setlist header → always drop
    if _TRACKLIST_WORD_RE.search(raw):
        return True

    ts_count = count_timestamps_in_comment(raw)
    if ts_count >= 3:
        return True

    pipe_rows = _PIPE_TRACK_ROW_RE.findall(raw)
    if len(pipe_rows) >= 2:
        return True

    track_lines = _TRACKLIST_LINE_RE.findall(raw)
    if len(track_lines) >= 3:
        return True

    # Dense "time + title" spam on one long line
    if ts_count >= 2 and len(raw) >= 220:
        return True

    # Lots of song separators with multiple times
    dash_hits = len(re.findall(r"\s[–—-]\s", raw))
    if ts_count >= 2 and dash_hits >= 3:
        return True

    # Pipe + song-title rows even when the scrape is truncated
    if pipe_rows and (dash_hits >= 1 or " - " in raw or " – " in raw):
        if ts_count >= 1 and len(raw) >= 80:
            return True

    return False


def extract_timestamp_from_comment(text: str) -> float | None:
    """Return a reaction-style timestamp (seconds), or None for tracklists."""
    if not text or not text.strip():
        return None
    if is_tracklist_comment(text):
        return None

    # Prefer timestamps near the start ("41:13 fire!" form).
    head = text.strip()[:160]
    for chunk in (head, text):
        for match in _TIMESTAMP_IN_TEXT_RE.finditer(chunk):
            secs = parse_timestamp_seconds(match.group(0))
            if secs is None or secs < 0:
                continue
            # Skip bare intro markers on long comments
            if secs < 5 and len(text.strip()) > 80:
                continue
            return float(secs)
    return None


def _seconds_to_hhmmss(secs: float) -> str:
    normalized = normalize_clip_timestamp(secs)
    if normalized:
        return normalized
    total = max(0, int(round(secs)))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def rank_comment_entries(
    comments: list[dict[str, Any]],
    *,
    top_n: int = DEFAULT_TOP_N,
    duration_sec: int = DEFAULT_ENTRY_DURATION_SEC,
    video_duration: float | None = None,
) -> list[dict[str, Any]]:
    """
    Keep comments that contain a timestamp, sort by likes, dedupe nearby times.

    Nearby = within 8 seconds of an already-kept entry (keeps the higher-liked one).
    """
    top_n = max(1, min(int(top_n or DEFAULT_TOP_N), 10))
    duration_sec = max(5, min(int(duration_sec or DEFAULT_ENTRY_DURATION_SEC), 60))

    scored: list[dict[str, Any]] = []
    for raw in comments or []:
        if not isinstance(raw, dict):
            continue
        fields = _comment_fields(raw)
        if not fields["text"]:
            continue
        if is_tracklist_comment(fields["text"]):
            continue
        entry_secs = extract_timestamp_from_comment(fields["text"])
        if entry_secs is None:
            continue
        if video_duration is not None and entry_secs >= float(video_duration):
            continue

        end_secs = entry_secs + duration_sec
        if video_duration is not None:
            end_secs = min(end_secs, float(video_duration))
            if end_secs <= entry_secs + 2:
                continue

        scored.append({
            **fields,
            "entry_seconds": entry_secs,
            "entry_timestamp": _seconds_to_hhmmss(entry_secs),
            "end_timestamp": _seconds_to_hhmmss(end_secs),
            "duration_sec": int(round(end_secs - entry_secs)),
        })

    scored.sort(key=lambda c: (-c["like_count"], c["entry_seconds"]))

    # Dedupe similar timestamps — keep highest-liked first.
    picked: list[dict[str, Any]] = []
    for cand in scored:
        if any(abs(cand["entry_seconds"] - p["entry_seconds"]) < 8 for p in picked):
            continue
        picked.append(cand)
        if len(picked) >= top_n:
            break

    for i, cand in enumerate(picked, start=1):
        cand["rank"] = i
        likes = int(cand.get("like_count") or 0)
        cand["likes"] = likes
        cand["like_count"] = likes
        cand["likes_label"] = _format_likes(likes)
    return picked


def _scored_timestamped_comments(
    comments: list[dict[str, Any]],
    *,
    video_duration: float | None = None,
) -> list[dict[str, Any]]:
    """Normalize comments that carry a usable reaction timestamp."""
    scored: list[dict[str, Any]] = []
    for raw in comments or []:
        if not isinstance(raw, dict):
            continue
        fields = _comment_fields(raw)
        if not fields["text"]:
            continue
        if is_tracklist_comment(fields["text"]):
            continue
        entry_secs = extract_timestamp_from_comment(fields["text"])
        if entry_secs is None:
            continue
        if video_duration is not None and entry_secs >= float(video_duration):
            continue
        scored.append({
            **fields,
            "entry_seconds": float(entry_secs),
            "entry_timestamp": _seconds_to_hhmmss(entry_secs),
        })
    return scored


def cluster_comment_entries(
    comments: list[dict[str, Any]],
    *,
    top_n: int = DISCOVERY_PREVIEW_COUNT,
    duration_sec: int = DEFAULT_ENTRY_DURATION_SEC,
    cluster_gap_sec: float = DEFAULT_CLUSTER_GAP_SEC,
    max_clip_sec: int = MAX_CLIP_DURATION_SEC,
    video_duration: float | None = None,
) -> list[dict[str, Any]]:
    """
    Group nearby timestamped comments into clip windows.

    Comments whose timestamps fall within ``cluster_gap_sec`` of the previous
    member of a growing cluster are merged. Window = min(ts) → min(max(ts)+duration,
    min_ts+max_clip, video_end). Clusters are ranked by sum of likes (then peak likes).
    """
    top_n = max(1, min(int(top_n or DISCOVERY_PREVIEW_COUNT), 10))
    duration_sec = max(5, min(int(duration_sec or DEFAULT_ENTRY_DURATION_SEC), 60))
    gap = max(1.0, float(cluster_gap_sec or DEFAULT_CLUSTER_GAP_SEC))
    max_clip = max(duration_sec, min(int(max_clip_sec or MAX_CLIP_DURATION_SEC), 60))

    scored = _scored_timestamped_comments(comments, video_duration=video_duration)
    if not scored:
        return []

    scored.sort(key=lambda c: c["entry_seconds"])

    raw_clusters: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = [scored[0]]
    for cand in scored[1:]:
        prev_ts = current[-1]["entry_seconds"]
        if cand["entry_seconds"] - prev_ts <= gap:
            current.append(cand)
        else:
            raw_clusters.append(current)
            current = [cand]
    raw_clusters.append(current)

    clusters: list[dict[str, Any]] = []
    for members in raw_clusters:
        members_sorted = sorted(members, key=lambda c: (-c["like_count"], c["entry_seconds"]))
        primary = members_sorted[0]
        start_secs = min(m["entry_seconds"] for m in members)
        last_ts = max(m["entry_seconds"] for m in members)
        end_secs = last_ts + duration_sec
        end_secs = min(end_secs, start_secs + max_clip)
        if video_duration is not None:
            end_secs = min(end_secs, float(video_duration))
        if end_secs <= start_secs + 2:
            continue

        likes_sum = sum(int(m.get("like_count") or 0) for m in members)
        likes_peak = int(primary.get("like_count") or 0)
        comment_payload = [
            {
                "text": m["text"],
                "like_count": int(m.get("like_count") or 0),
                "likes": int(m.get("like_count") or 0),
                "author": m.get("author"),
                "comment_id": m.get("comment_id") or "",
                "entry_seconds": m["entry_seconds"],
                "entry_timestamp": m["entry_timestamp"],
            }
            for m in members_sorted
        ]
        clusters.append({
            "text": primary["text"],
            "like_count": likes_sum,
            "likes": likes_sum,
            "likes_peak": likes_peak,
            "likes_label": _format_likes(likes_sum),
            "author": primary.get("author"),
            "comment_id": primary.get("comment_id") or "",
            "entry_seconds": start_secs,
            "entry_timestamp": _seconds_to_hhmmss(start_secs),
            "end_timestamp": _seconds_to_hhmmss(end_secs),
            "duration_sec": int(round(end_secs - start_secs)),
            "comments": comment_payload,
            "cluster_size": len(members),
        })

    clusters.sort(key=lambda c: (-c["like_count"], -c.get("likes_peak", 0), c["entry_seconds"]))
    picked = clusters[:top_n]
    for i, cand in enumerate(picked, start=1):
        cand["rank"] = i
    return picked


def find_clustered_comment_entries(
    url: str,
    *,
    top_n: int = DISCOVERY_PREVIEW_COUNT,
    duration_sec: int = DEFAULT_ENTRY_DURATION_SEC,
    cluster_gap_sec: float = DEFAULT_CLUSTER_GAP_SEC,
    max_comments: int = DEFAULT_MAX_COMMENTS,
) -> dict[str, Any]:
    """Fetch comments and return time-clustered clip candidates (discovery path)."""
    comments, video_duration, video_title, err = fetch_youtube_comments(
        url, max_comments=max_comments
    )
    if err:
        return {"success": False, "error": err, "entries": [], "video_title": video_title}

    entries = cluster_comment_entries(
        comments,
        top_n=top_n,
        duration_sec=duration_sec,
        cluster_gap_sec=cluster_gap_sec,
        video_duration=video_duration,
    )
    if not entries:
        return {
            "success": True,
            "entries": [],
            "scanned": len(comments),
            "video_title": video_title,
            "message": (
                "No reaction-style timestamped comments were found "
                "(tracklists are filtered out)."
            ),
        }

    return {
        "success": True,
        "entries": entries,
        "scanned": len(comments),
        "video_duration": video_duration,
        "duration_sec": duration_sec,
        "cluster_gap_sec": cluster_gap_sec,
        "video_title": video_title,
    }


def _format_likes(n: int) -> str:
    n = max(0, int(n or 0))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


def _merge_comment_lists(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe by comment id, keeping the highest like_count."""
    by_id: dict[str, dict[str, Any]] = {}
    orphans: list[dict[str, Any]] = []
    for group in groups:
        for raw in group or []:
            if not isinstance(raw, dict):
                continue
            cid = str(raw.get("id") or raw.get("comment_id") or "")
            if not cid:
                orphans.append(raw)
                continue
            prev = by_id.get(cid)
            if prev is None:
                by_id[cid] = raw
                continue
            prev_likes = _parse_like_count(prev.get("like_count", prev.get("likes")))
            new_likes = _parse_like_count(raw.get("like_count", raw.get("likes")))
            if new_likes > prev_likes:
                by_id[cid] = raw
    return list(by_id.values()) + orphans


def fetch_youtube_comments(
    url: str,
    *,
    max_comments: int = DEFAULT_MAX_COMMENTS,
) -> tuple[list[dict[str, Any]], float | None, str | None, str | None]:
    """
    Fetch comments via yt-dlp (no download).

    Merges top + newest sorts so timestamped hits keep real like counts.
    Returns (comments, video_duration_seconds, video_title, error_message).
    """
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return [], None, None, "A YouTube URL is required to scrape comments."

    try:
        import yt_dlp
    except ImportError:
        return [], None, None, "yt-dlp is not installed."

    max_comments = max(20, min(int(max_comments or DEFAULT_MAX_COMMENTS), 500))
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    per_sort = max(40, max_comments // 2)

    def _extract(sort: str) -> tuple[list[dict[str, Any]], float | None, str | None, str | None]:
        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "getcomments": True,
            "extractor_args": {
                "youtube": {
                    # web_safari avoids the missing-formats wall on datacenter IPs
                    "player_client": ["web_safari"],
                    "max_comments": [str(per_sort)],
                    "comment_sort": [sort],
                }
            },
        }
        # YouTube blocks datacenter IPs ("Sign in to confirm you're not a bot").
        # When a cookies.txt from a logged-in browser is available, use it.
        cookies_file = os.environ.get("YT_COOKIES_FILE", "").strip()
        if cookies_file and os.path.isfile(cookies_file):
            ydl_opts["cookiefile"] = cookies_file
        # Node solves YouTube's JS challenge so yt-dlp can mint PO tokens,
        # which the web player now requires to serve formats.
        if shutil.which("node"):
            ydl_opts["js_runtimes"] = {"node": {}}
            ydl_opts["remote_components"] = ["ejs:github"]
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(watch_url, download=False)
        except Exception as exc:
            return [], None, None, f"Failed to fetch comments: {exc}"

        if not info:
            return [], None, None, "No video info returned."
        comments = info.get("comments") or []
        if not isinstance(comments, list):
            comments = []
        duration = info.get("duration")
        try:
            video_duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            video_duration = None
        video_title = str(info.get("title") or "").strip() or None
        return comments, video_duration, video_title, None

    top_comments, video_duration, video_title, err_top = _extract("top")
    new_comments, video_duration_new, video_title_new, err_new = _extract("new")

    if not top_comments and not new_comments:
        return [], None, None, err_top or err_new or "No comments returned."

    comments = _merge_comment_lists(top_comments, new_comments)
    if video_duration is None:
        video_duration = video_duration_new
    if not video_title:
        video_title = video_title_new
    return comments, video_duration, video_title, None


def find_comment_entry_points(
    url: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    duration_sec: int = DEFAULT_ENTRY_DURATION_SEC,
    max_comments: int = DEFAULT_MAX_COMMENTS,
) -> dict[str, Any]:
    """High-level helper used by the web API."""
    comments, video_duration, video_title, err = fetch_youtube_comments(url, max_comments=max_comments)
    if err:
        return {"success": False, "error": err, "entries": [], "video_title": video_title}

    entries = rank_comment_entries(
        comments,
        top_n=top_n,
        duration_sec=duration_sec,
        video_duration=video_duration,
    )
    if not entries:
        return {
            "success": True,
            "entries": [],
            "scanned": len(comments),
            "video_title": video_title,
            "message": (
                "No reaction-style timestamped comments were found "
                "(tracklists are filtered out). "
                "Try a set with comments like \"41:13 this drop!\""
            ),
        }

    return {
        "success": True,
        "entries": entries,
        "scanned": len(comments),
        "video_duration": video_duration,
        "duration_sec": duration_sec,
        "video_title": video_title,
    }
