#!/usr/bin/env python3
"""config.py — Environment and path resolution for analytics (inside posting_clips)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Package dir = posting_clips/analytics ; host root = posting_clips
PACKAGE_ROOT = Path(__file__).resolve().parent
HOST_ROOT = PACKAGE_ROOT.parent

load_dotenv(dotenv_path=HOST_ROOT / ".env", override=False)

# Platforms included in analytics pulls. TikTok disabled until post URLs are fixed.
# NOTE: one-item tuples need a trailing comma — ("youtube") is just a string.
ANALYTICS_PLATFORMS = ("youtube",)


def _resolve_path(raw: str | None, default: Path) -> Path:
    if not raw or not str(raw).strip():
        return default.resolve()
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        p = (HOST_ROOT / p).resolve()
    return p


@lru_cache(maxsize=1)
def get_settings() -> dict[str, Any]:
    clients_path = _resolve_path(
        os.environ.get("CLIENTS_JSON_PATH"),
        HOST_ROOT / "clients.json",
    )
    ig_session_dir = _resolve_path(
        os.environ.get("INSTAGRAM_SESSION_DIR"),
        HOST_ROOT / "instagram_browser",
    )
    tt_session_dir = _resolve_path(
        os.environ.get("TIKTOK_SESSION_DIR"),
        HOST_ROOT / "tiktok_session",
    )
    exports_dir = _resolve_path(
        os.environ.get("EXPORTS_DIR"),
        PACKAGE_ROOT / "exports",
    )

    return {
        "posting_clips_dir": HOST_ROOT,
        "clients_json_path": clients_path,
        "instagram_session_dir": ig_session_dir,
        "tiktok_session_dir": tt_session_dir,
        "exports_dir": exports_dir,
        "supabase_url": os.environ.get("SUPABASE_URL", "").strip(),
        "supabase_key": (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
            or os.environ.get("SUPABASE_ANON_KEY", "").strip()
        ),
        "youtube_api_key": os.environ.get("YOUTUBE_API_KEY", "").strip(),
        "poll_interval_seconds": int(
            os.environ.get(
                "ANALYTICS_POLL_SECONDS",
                os.environ.get("POLL_INTERVAL_SECONDS", "3600"),
            )
        ),
        "auto_export_json": os.environ.get("AUTO_EXPORT_JSON", "true").lower()
        in ("1", "true", "yes"),
        "enable_background_worker": os.environ.get(
            "ENABLE_ANALYTICS_WORKER", "false"
        ).lower()
        in ("1", "true", "yes"),
    }


def load_clients_config() -> dict[str, Any]:
    """Load clients.json from the poster host."""
    settings = get_settings()
    path: Path = settings["clients_json_path"]
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_instagram_sessionid() -> str | None:
    """Resolve Instagram sessionid from env or clients.json."""
    sid = os.environ.get("INSTAGRAM_SESSIONID", "").strip()
    if sid:
        return sid
    cfg = load_clients_config()
    ig = cfg.get("instagram") or {}
    if isinstance(ig, dict):
        if ig.get("sessionid"):
            return str(ig["sessionid"]).strip() or None
        auth = ig.get("authorization_data")
        if isinstance(auth, dict) and auth.get("sessionid"):
            return str(auth["sessionid"]).strip() or None
    settings = get_settings()
    session_file = settings["posting_clips_dir"] / "instagram_session.json"
    if session_file.is_file():
        try:
            with open(session_file, encoding="utf-8") as f:
                blob = json.load(f)
            auth = blob.get("authorization_data") or {}
            if isinstance(auth, dict) and auth.get("sessionid"):
                return str(auth["sessionid"]).strip() or None
        except Exception:
            pass
    return None


def credential_status() -> dict[str, Any]:
    """Lightweight checks for /health (no interactive login)."""
    settings = get_settings()
    clients = load_clients_config()
    yt = clients.get("youtube") or {}
    token = yt.get("token") if isinstance(yt.get("token"), dict) else {}
    has_yt_token = bool(token.get("token") or token.get("refresh_token"))
    token_file = settings["posting_clips_dir"] / "token.json"
    ig_dir: Path = settings["instagram_session_dir"]
    tt_dir: Path = settings["tiktok_session_dir"]

    yt_scopes_raw = token.get("scopes") or token.get("scope") or []
    if isinstance(yt_scopes_raw, str):
        yt_scopes = set(yt_scopes_raw.split())
    elif isinstance(yt_scopes_raw, list):
        yt_scopes = set(yt_scopes_raw)
    else:
        yt_scopes = set()
    has_yt_analytics = (
        "https://www.googleapis.com/auth/yt-analytics.readonly" in yt_scopes
    )

    return {
        "clients_json": settings["clients_json_path"].is_file(),
        "youtube_api_key": bool(settings["youtube_api_key"]),
        "youtube_oauth_token": has_yt_token
        or (token_file.is_file() and token_file.stat().st_size > 0),
        "youtube_analytics_scope": has_yt_analytics,
        "instagram_sessionid": bool(get_instagram_sessionid()),
        "instagram_session_dir": ig_dir.is_dir() and any(ig_dir.iterdir())
        if ig_dir.is_dir()
        else False,
        "tiktok_session_dir": tt_dir.is_dir() and any(tt_dir.iterdir())
        if tt_dir.is_dir()
        else False,
    }
