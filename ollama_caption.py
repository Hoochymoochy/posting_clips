#!/usr/bin/env python3
"""Rewrite YouTube comments into punchy, personality-heavy social captions via Ollama."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_CAPTION_MODEL", "llama3.1:latest")

_SYSTEM_PROMPT = """You write short TikTok/Reels captions for DJ-set clips.

Voice:
- Fun, hype, a little chaotic — like a hyped friend or club MC
- Can lean into "ladies and gentlemen…" / "Ibiza nights…" energy when it fits the set title
- Playful, not corporate. Never corny corporate marketing.

Rules:
- Use the SET TITLE for context (artist, venue, vibe) — weave it in naturally
- Use the COMMENT for the moment energy — keep the spirit, don't quote it word-for-word
- Strip timestamps, @usernames, and raw tracklist junk
- 1–2 short sentences max
- No hashtags unless they were already in the comment
- No emojis unless one really slaps
- Output ONLY the caption — no labels, quotes, or reasoning"""


def _normalize_ollama_host(raw: str | None) -> str:
    """Accept full URLs or host/port fragments from OLLAMA_HOST."""
    text = (raw or "").strip() or "http://127.0.0.1:11434"
    if text in ("0.0.0.0", "::", "[::]"):
        text = "127.0.0.1:11434"
    if not re.match(r"^https?://", text, re.I):
        text = "http://" + text.lstrip("/")
    try:
        from urllib.parse import urlparse

        parsed = urlparse(text)
        if parsed.hostname and parsed.path in ("", "/") and ":" not in (parsed.netloc or ""):
            text = f"{parsed.scheme}://{parsed.hostname}:11434"
    except Exception:
        pass
    return text.rstrip("/")


DEFAULT_OLLAMA_HOST = _normalize_ollama_host(os.environ.get("OLLAMA_HOST"))


def _clean_caption(text: str) -> str:
    out = (text or "").strip()
    out = re.sub(r"<think>[\s\S]*?</think>", "", out, flags=re.I).strip()

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    reasoning_hint = re.compile(
        r"^(we are|the task|steps?:|1\.|comment:|rewrite|output only|here|caption:)",
        re.I,
    )
    content_lines = [ln for ln in lines if not reasoning_hint.match(ln)]
    if content_lines:
        out = content_lines[-1]
    elif lines:
        out = lines[-1]

    for prefix in ("caption:", "here is", "here's", "rewritten:", "output:"):
        lower = out.lower()
        if lower.startswith(prefix):
            out = out[len(prefix):].lstrip(" :-")

    if (out.startswith('"') and out.endswith('"')) or (out.startswith("'") and out.endswith("'")):
        out = out[1:-1].strip()

    if len(out) > 320 or reasoning_hint.match(out):
        return ""
    return out.strip()


def _build_user_prompt(
    comment_text: str,
    *,
    video_title: str = "",
    entry_timestamp: str = "",
    extra_comments: list[str] | None = None,
) -> str:
    parts = []
    title = (video_title or "").strip()
    if title:
        parts.append(f"SET TITLE: {title}")
    ts = (entry_timestamp or "").strip()
    if ts:
        parts.append(f"MOMENT TIME: {ts}")
    parts.append(f"FAN COMMENT: {(comment_text or '').strip()}")
    extras = [str(c).strip() for c in (extra_comments or []) if str(c).strip()]
    # Avoid repeating the primary comment
    primary_lower = (comment_text or "").strip().lower()
    extras = [c for c in extras if c.lower() != primary_lower][:4]
    if extras:
        parts.append("NEARBY FAN COMMENTS (same moment):")
        for i, extra in enumerate(extras, start=1):
            parts.append(f"  {i}. {extra}")
    parts.append(
        "Write one fun, hype caption for this clip. "
        "Lean on the set title for personality (artist/venue vibe)."
        + (" Blend the nearby comments into one vibe — don't list them." if extras else "")
    )
    return "\n".join(parts)


def rewrite_caption_with_ollama(
    comment_text: str,
    *,
    video_title: str = "",
    entry_timestamp: str = "",
    extra_comments: list[str] | None = None,
    model: str | None = None,
    host: str | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """
    Call local Ollama /api/chat and return {success, caption, ...}.

    Falls back to a light template (or raw comment) if Ollama is down.
    ``extra_comments`` are sibling reactions from a time cluster (optional).
    """
    raw = (comment_text or "").strip()
    title = (video_title or "").strip()
    if not raw and not title:
        return {"success": False, "error": "No comment or title to rewrite.", "caption": ""}

    fallback = _personality_fallback(raw, title)

    model_name = (model or DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
    base = _normalize_ollama_host(host or DEFAULT_OLLAMA_HOST)
    url = f"{base}/api/chat"

    body: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_user_prompt(
                    raw or "this drop hits different",
                    video_title=title,
                    entry_timestamp=entry_timestamp,
                    extra_comments=extra_comments,
                ),
            },
        ],
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.9,
            "num_predict": 100,
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        hint = (
            f"HTTP {exc.code} from Ollama. "
            f"Is model '{model_name}' installed? Try: ollama pull {model_name}"
        )
        if detail:
            hint = f"{hint} ({detail})"
        return {
            "success": False,
            "error": hint,
            "caption": fallback,
            "fallback": True,
            "model": model_name,
        }
    except urllib.error.URLError as exc:
        return {
            "success": False,
            "error": (
                f"Ollama is not reachable at {base}. "
                f"Start Ollama and pull model '{model_name}'. ({exc})"
            ),
            "caption": fallback,
            "fallback": True,
        }
    except TimeoutError:
        return {
            "success": False,
            "error": f"Ollama timed out after {timeout_sec}s.",
            "caption": fallback,
            "fallback": True,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Ollama request failed: {exc}",
            "caption": fallback,
            "fallback": True,
        }

    message = payload.get("message") or {}
    generated = _clean_caption(str(message.get("content") or payload.get("response") or ""))
    if not generated:
        return {
            "success": False,
            "error": "Ollama returned an empty caption.",
            "caption": fallback,
            "fallback": True,
            "model": model_name,
        }

    return {
        "success": True,
        "caption": generated,
        "model": model_name,
        "source_comment": raw,
        "video_title": title,
    }


def _personality_fallback(comment: str, title: str) -> str:
    """Lightweight caption when Ollama is unavailable."""
    cleaned = re.sub(
        r"(?<![\d.])(?:\d{1,2}:)?\d{1,3}:\d{2}(?![\d.])",
        "",
        comment or "",
    ).strip(" -–—|,")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    short_title = title.strip()
    if len(short_title) > 60:
        short_title = short_title[:57] + "…"

    if short_title and cleaned:
        return f"Ladies and gentlemen... {short_title}. {cleaned}"
    if short_title:
        return f"Ladies and gentlemen... {short_title}. This one's a moment."
    return cleaned or "This drop hits different."
