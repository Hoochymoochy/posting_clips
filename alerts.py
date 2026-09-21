#!/usr/bin/env python3
"""
alerts.py — Discord notifications for the clip poster worker.

Alert matrix (default):
  One temporary upload failure    → log only
  Three consecutive failures      → Discord warning
  Authentication expired          → Discord immediately
  Worker stops responding         → Critical alert
  Storage exceeds 85%             → Warning
  Supabase unavailable repeatedly → Critical alert
  Job stuck processing            → Warning
  Unknown publishing outcome      → Requires attention
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# --- Event keys (stable IDs for dedupe / matrix) ---
EVENT_TEMP_FAILURE = "temp_upload_failure"
EVENT_THREE_FAILURES = "three_consecutive_failures"
EVENT_AUTH_EXPIRED = "auth_expired"
EVENT_WORKER_DEAD = "worker_stops_responding"
EVENT_STORAGE_HIGH = "storage_exceeds_85"
EVENT_SUPABASE_DOWN = "supabase_unavailable"
EVENT_JOB_STUCK = "job_stuck_processing"
EVENT_UNKNOWN_OUTCOME = "unknown_publishing_outcome"

# severity → Discord embed color
_COLORS = {
    "log": 0x95A5A6,
    "warning": 0xF1C40F,
    "critical": 0xE74C3C,
    "attention": 0xE67E22,
}

_ALERT_MATRIX: dict[str, dict[str, Any]] = {
    EVENT_TEMP_FAILURE: {
        "label": "One temporary upload failure",
        "discord": False,
        "severity": "log",
    },
    EVENT_THREE_FAILURES: {
        "label": "Three consecutive failures",
        "discord": True,
        "severity": "warning",
    },
    EVENT_AUTH_EXPIRED: {
        "label": "Authentication expired",
        "discord": True,
        "severity": "critical",
        "immediate": True,
    },
    EVENT_WORKER_DEAD: {
        "label": "Worker stops responding",
        "discord": True,
        "severity": "critical",
    },
    EVENT_STORAGE_HIGH: {
        "label": "Storage exceeds 85%",
        "discord": True,
        "severity": "warning",
    },
    EVENT_SUPABASE_DOWN: {
        "label": "Supabase unavailable repeatedly",
        "discord": True,
        "severity": "critical",
    },
    EVENT_JOB_STUCK: {
        "label": "Job stuck processing",
        "discord": True,
        "severity": "warning",
    },
    EVENT_UNKNOWN_OUTCOME: {
        "label": "Unknown publishing outcome",
        "discord": True,
        "severity": "attention",
    },
}

_AUTH_MARKERS = (
    "session expired",
    "not logged in",
    "invalid_grant",
    "refresh_token",
    "token has been expired",
    "token expired",
    "unauthorized",
    "authentication",
    "oauth",
    "re-login",
    "requires a one-time browser login",
    "client secrets",
    "credentials do not contain",
)

_PERMANENT_MARKERS = (
    "auto-post blocked",
    "safety validator",
    "invalid video",
    "unsupported",
    "too long",
    "too short",
    "aspect ratio",
)

_DEDUPE_PATH = Path(os.environ.get("ALERT_DEDUPE_FILE", ".alert_dedupe.json"))
_DEFAULT_DEDUPE_SECONDS = int(os.environ.get("ALERT_DEDUPE_SECONDS", "1800"))


def _webhook_url() -> str:
    return (
        os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        or os.environ.get("DISCORD_BOT_WEBHOOK", "").strip()
    )


def classify_outcome(outcome: dict[str, Any] | None) -> str:
    """
    Return one of: success | auth | transient | permanent | uncertain
    """
    if not outcome:
        return "transient"
    if outcome.get("uncertain"):
        return "uncertain"
    if outcome.get("success"):
        return "success"
    kind = (outcome.get("error_kind") or "").strip().lower()
    if kind in ("auth", "transient", "permanent", "uncertain"):
        return kind
    err = str(outcome.get("error") or "").lower()
    if any(m in err for m in _AUTH_MARKERS):
        return "auth"
    if any(m in err for m in _PERMANENT_MARKERS):
        return "permanent"
    if "may still have published" in err or "did not confirm" in err:
        return "uncertain"
    return "transient"


def _load_dedupe() -> dict[str, float]:
    if not _DEDUPE_PATH.is_file():
        return {}
    try:
        data = json.loads(_DEDUPE_PATH.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (data or {}).items()}
    except Exception:
        return {}


def _save_dedupe(state: dict[str, float]) -> None:
    try:
        # Drop stale entries (> 7 days)
        cutoff = time.time() - 7 * 86400
        trimmed = {k: v for k, v in state.items() if v >= cutoff}
        _DEDUPE_PATH.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  [WARN] Could not save alert dedupe state: {e}")


def _should_send(dedupe_key: str | None, dedupe_seconds: int) -> bool:
    if not dedupe_key:
        return True
    state = _load_dedupe()
    last = state.get(dedupe_key)
    now = time.time()
    if last is not None and (now - last) < dedupe_seconds:
        return False
    state[dedupe_key] = now
    _save_dedupe(state)
    return True


def send_discord(
    content: str,
    *,
    title: str | None = None,
    severity: str = "warning",
    fields: list[dict[str, str]] | None = None,
    dedupe_key: str | None = None,
    dedupe_seconds: int | None = None,
) -> bool:
    """Post a message to the Discord webhook. Returns True if sent."""
    url = _webhook_url()
    if not url:
        print(f"  [ALERT] (no DISCORD_WEBHOOK_URL) {title or content}")
        return False

    if not _should_send(dedupe_key, dedupe_seconds if dedupe_seconds is not None else _DEFAULT_DEDUPE_SECONDS):
        print(f"  [ALERT] Suppressed duplicate: {dedupe_key}")
        return False

    embed: dict[str, Any] = {
        "title": title or "Clip Poster Alert",
        "description": content[:4000],
        "color": _COLORS.get(severity, _COLORS["warning"]),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = [
            {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", False)}
            for f in fields
            if f.get("name")
        ]

    payload = {
        "username": os.environ.get("DISCORD_BOT_NAME", "Clip Poster"),
        "embeds": [embed],
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "clip-poster/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
            if ok:
                print(f"  [ALERT] Discord notified: {title or content[:60]}")
            return ok
    except urllib.error.HTTPError as e:
        print(f"  [WARN] Discord webhook HTTP {e.code}: {e.read()[:200]!r}")
        return False
    except Exception as e:
        print(f"  [WARN] Discord webhook failed: {e}")
        return False


def emit(
    event: str,
    *,
    detail: str,
    clip_id: str | None = None,
    platform: str | None = None,
    extra_fields: list[dict[str, str]] | None = None,
    force_discord: bool | None = None,
    dedupe_key: str | None = None,
) -> None:
    """
    Emit an alert according to the matrix.
    Temporary failures are log-only unless force_discord is True.
    """
    meta = _ALERT_MATRIX.get(event, {
        "label": event,
        "discord": True,
        "severity": "attention",
    })
    label = meta["label"]
    severity = meta.get("severity", "warning")
    use_discord = meta.get("discord", True) if force_discord is None else force_discord

    line = f"[{severity.upper()}] {label}: {detail}"
    if platform:
        line = f"[{severity.upper()}] {label} ({platform}): {detail}"
    print(f"  [ALERT] {line}")

    if not use_discord:
        return

    fields: list[dict[str, str]] = []
    if clip_id:
        fields.append({"name": "Clip", "value": str(clip_id), "inline": True})
    if platform:
        fields.append({"name": "Platform", "value": str(platform), "inline": True})
    fields.append({"name": "Event", "value": label, "inline": False})
    if extra_fields:
        fields.extend(extra_fields)

    key = dedupe_key or f"{event}:{platform or '-'}:{clip_id or '-'}"
    # Auth alerts: shorter dedupe so reconnect notices still fire after a while
    dedupe_s = 600 if event == EVENT_AUTH_EXPIRED else _DEFAULT_DEDUPE_SECONDS

    send_discord(
        detail,
        title=f"{'🚨 ' if severity == 'critical' else ''}{label}",
        severity=severity,
        fields=fields,
        dedupe_key=key,
        dedupe_seconds=dedupe_s,
    )


def alert_for_outcome(
    *,
    clip_id: str,
    platform: str,
    outcome: dict[str, Any],
    attempt: int,
    max_attempts: int,
) -> str:
    """
    Classify outcome, emit the right alert, return the kind.
    """
    kind = classify_outcome(outcome)
    err = str(outcome.get("error") or "")

    if kind == "success":
        return kind

    if kind == "auth":
        emit(
            EVENT_AUTH_EXPIRED,
            detail=err or f"{platform} authentication failed",
            clip_id=clip_id,
            platform=platform,
            extra_fields=[
                {
                    "name": "Action",
                    "value": "Reconnect via `/api/connections` or `python uploader.py --setup-{platform}`",
                }
            ],
        )
        return kind

    if kind == "uncertain":
        emit(
            EVENT_UNKNOWN_OUTCOME,
            detail=(
                err
                or "Post/Share was clicked but publish was not confirmed. "
                "Do not auto-retry — check the account manually to avoid a double post."
            ),
            clip_id=clip_id,
            platform=platform,
            extra_fields=[
                {"name": "Safety", "value": "Auto-retry disabled for this channel"},
            ],
        )
        return kind

    if kind == "permanent":
        emit(
            EVENT_THREE_FAILURES if attempt >= max_attempts else EVENT_TEMP_FAILURE,
            detail=err or "Permanent upload failure",
            clip_id=clip_id,
            platform=platform,
            force_discord=attempt >= max_attempts,
        )
        return kind

    # transient
    if attempt >= max_attempts:
        emit(
            EVENT_THREE_FAILURES,
            detail=err or f"{platform} failed {attempt} times",
            clip_id=clip_id,
            platform=platform,
            extra_fields=[{"name": "Attempts", "value": f"{attempt}/{max_attempts}"}],
        )
    else:
        emit(
            EVENT_TEMP_FAILURE,
            detail=err or f"{platform} temporary failure (attempt {attempt}/{max_attempts})",
            clip_id=clip_id,
            platform=platform,
        )
    return kind
