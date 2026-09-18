#!/usr/bin/env python3
"""
connections.py — Track and set up YouTube / Instagram / TikTok credentials
on the poster backend (where the worker posts from).

Credentials live on the poster host filesystem:
  - YouTube:  token.json (+ client_secrets.json for OAuth)
  - Instagram: instagram_browser/ (Playwright profile)
  - TikTok:    tiktok_session/ (Playwright profile)
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import traceback
from datetime import datetime, timezone
from typing import Any

from uploader import load_config, setup_instagram_interactive, setup_tiktok_interactive
from uploader import (
    _persist_youtube_creds,
    load_config,
    resolve_config_path,
    save_clients_config,
    setup_instagram_interactive,
    setup_tiktok_interactive,
)

# In-flight interactive setups (browser login on this host)
_setup_lock = threading.Lock()
_setup_state: dict[str, dict[str, Any]] = {
    "youtube": {"running": False, "message": "", "error": None, "finished_at": None},
    "instagram": {"running": False, "message": "", "error": None, "finished_at": None},
    "tiktok": {"running": False, "message": "", "error": None, "finished_at": None},
}


def _dir_nonempty(path: str) -> bool:
    return os.path.isdir(path) and bool(os.listdir(path))


def _file_nonempty(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _youtube_paths(cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or load_config()
    yt = cfg.get("youtube", {})
    secrets = yt.get("client_secrets_file", "client_secrets.json")
    token = yt.get("token_file", "token.json")
    if not os.path.isfile(secrets):
        if os.path.isfile(f"{secrets}.json"):
            secrets = f"{secrets}.json"
        elif os.path.isfile("client_secrets.json.json"):
            secrets = "client_secrets.json.json"
    return secrets, token


def _instagram_paths(cfg: dict | None = None) -> tuple[str, str]:
    cfg = cfg or load_config()
    ig = cfg.get("instagram", {})
    return (
        ig.get("session_dir", "instagram_browser"),
        ig.get("session_file", "instagram_session.json"),
    )


def _tiktok_path(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    return cfg.get("tiktok", {}).get("session_dir", "tiktok_session")


def youtube_connected(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    yt = cfg.get("youtube", {})
    if isinstance(yt.get("token"), dict) and (yt["token"].get("token") or yt["token"].get("refresh_token")):
        return True
    _, token = _youtube_paths(cfg)
    return _file_nonempty(token)


def youtube_can_connect(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    yt = cfg.get("youtube", {})
    if isinstance(yt.get("client_secrets"), dict) or youtube_connected(cfg):
        return True
    secrets, _ = _youtube_paths(cfg)
    return os.path.isfile(secrets)


def instagram_connected(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    session_dir, session_file = _instagram_paths(cfg)
    return _dir_nonempty(session_dir) or _file_nonempty(session_file)
    if _dir_nonempty(session_dir) or _file_nonempty(session_file):
        return True
    ig = cfg.get("instagram", {})
    if isinstance(ig.get("authorization_data"), dict) and ig["authorization_data"].get("sessionid"):
        return True
    if ig.get("sessionid"):
        return True
    return False


def tiktok_connected(cfg: dict | None = None) -> bool:
    return _dir_nonempty(_tiktok_path(cfg))


def get_setup_snapshot() -> dict[str, dict[str, Any]]:
    with _setup_lock:
        return {k: dict(v) for k, v in _setup_state.items()}


def get_connections_status() -> dict[str, Any]:
    """Return connected / ready state for each platform (poster host)."""
    cfg = load_config()
    secrets, token = _youtube_paths(cfg)
    ig_dir, ig_file = _instagram_paths(cfg)
    tt_dir = _tiktok_path(cfg)
    setups = get_setup_snapshot()

    yt_ok = youtube_connected(cfg)
    ig_ok = instagram_connected(cfg)
    tt_ok = tiktok_connected(cfg)

    return {
        "success": True,
        "host": "poster",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": {
            "youtube": {
                "connected": yt_ok,
                "can_connect": youtube_can_connect(cfg),
                "label": "YouTube Shorts",
                "detail": (
                    "OAuth token saved"
                    if yt_ok
                    else (
                        "client_secrets.json missing — add Google OAuth desktop credentials"
                        if not youtube_can_connect(cfg)
                        else "Not connected — click Connect to authorize"
                    )
                ),
                "token_file": token,
                "client_secrets_present": os.path.isfile(secrets),
                "token_file": "clients.json" if isinstance(cfg.get("youtube", {}).get("token"), dict) else token,
                "client_secrets_present": youtube_can_connect(cfg),
                "setup": setups["youtube"],
            },
            "instagram": {
                "connected": ig_ok,
                "can_connect": True,
                "label": "Instagram Reels",
                "detail": (
                    "Browser session saved"
                    if ig_ok
                    else "Not connected — click Connect and log in in the browser"
                ),
                "session_dir": ig_dir,
                "setup": setups["instagram"],
            },
            "tiktok": {
                "connected": tt_ok,
                "can_connect": True,
                "label": "TikTok",
                "detail": (
                    "Browser session saved"
                    if tt_ok
                    else "Not connected — click Connect and log in in the browser"
                ),
                "session_dir": tt_dir,
                "setup": setups["tiktok"],
            },
        },
        "all_connected": yt_ok and ig_ok and tt_ok,
        "any_connected": yt_ok or ig_ok or tt_ok,
    }


def setup_youtube_interactive(config_path: str = "config.json") -> dict[str, Any]:
    """Run Google OAuth once and persist token.json for the poster worker."""
def setup_youtube_interactive(config_path: str = "clients.json") -> dict[str, Any]:
    """Run Google OAuth once and persist token in clients.json (and token.json)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return {
            "success": False,
            "error": (
                "Missing Google API libraries. Run: "
                "pip install google-api-python-client google-auth-oauthlib"
            ),
        }

    cfg = load_config(config_path)
    yt = cfg.get("youtube", {})
    secrets, token_file = _youtube_paths(cfg)
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]

    if not os.path.isfile(secrets):
    has_client_secrets_dict = isinstance(yt.get("client_secrets"), dict)
    if not has_client_secrets_dict and not os.path.isfile(secrets):
        return {
            "success": False,
            "error": (
                f"YouTube client secrets not found ({secrets}).\n"
                f"YouTube client secrets not found in clients.json or ({secrets}).\n"
                "1. Google Cloud Console → enable YouTube Data API v3\n"
                "2. Create OAuth Desktop client\n"
                "3. Download JSON as client_secrets.json in posting_clips/"
                "3. Download JSON and save into clients.json (or as client_secrets.json in posting_clips/)"
            ),
        }

    creds = None
    if os.path.exists(token_file):
    if isinstance(yt.get("token"), dict):
        try:
            creds = Credentials.from_authorized_user_info(yt["token"], scopes)
        except Exception:
            creds = None

    if not creds and os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, scopes)
        except Exception:
            creds = None

    if creds and creds.valid:
        return {"success": True, "message": "YouTube already connected.", "token_file": token_file}

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            _persist_youtube_creds(creds, token_file=token_file, config_path=config_path)
            return {"success": True, "message": "YouTube token refreshed.", "token_file": token_file}
        except Exception:
            creds = None

    print("\n========================================================")
    print("YouTube OAuth Setup")
    print("========================================================")
    print("A browser window will open. Sign in and allow youtube.upload.\n")

    flow = InstalledAppFlow.from_client_secrets_file(secrets, scopes)
    if has_client_secrets_dict:
        flow = InstalledAppFlow.from_client_config(yt["client_secrets"], scopes)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(secrets, scopes)
    creds = flow.run_local_server(port=0)

    with open(token_file, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    _persist_youtube_creds(creds, token_file=token_file, config_path=config_path)

    print(f"[OK] YouTube token saved to {token_file}")
    return {"success": True, "message": f"YouTube connected. Token saved to {token_file}.", "token_file": token_file}
    print(f"[OK] YouTube token saved to clients.json / {token_file}")
    return {"success": True, "message": f"YouTube connected. Token saved.", "token_file": token_file}
    print(f"[OK] YouTube token saved to clients.json")
    print("[OK] YouTube token saved to clients.json")
    return {"success": True, "message": "YouTube connected. Token saved.", "token_file": token_file}


def _run_setup(platform: str) -> None:
    platform = platform.lower().strip()
    try:
        if platform == "youtube":
            result = setup_youtube_interactive()
            ok = bool(result.get("success"))
            msg = result.get("message") or result.get("error") or ""
            err = None if ok else (result.get("error") or msg)
        elif platform == "instagram":
            cfg = load_config()
            ig_dir, ig_file = _instagram_paths(cfg)
            setup_instagram_interactive(session_file=ig_file, session_dir=ig_dir)
            ok = instagram_connected(cfg)
            msg = "Instagram session saved." if ok else "Instagram setup finished but session not detected."
            err = None if ok else msg
        elif platform == "tiktok":
            cfg = load_config()
            setup_tiktok_interactive(_tiktok_path(cfg))
            ok = tiktok_connected(cfg)
            msg = "TikTok session saved." if ok else "TikTok setup finished but session not detected."
            err = None if ok else msg
        else:
            ok, msg, err = False, f"Unknown platform: {platform}", f"Unknown platform: {platform}"

        with _setup_lock:
            _setup_state[platform]["running"] = False
            _setup_state[platform]["message"] = msg
            _setup_state[platform]["error"] = err
            _setup_state[platform]["finished_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as e:
        with _setup_lock:
            _setup_state[platform]["running"] = False
            _setup_state[platform]["message"] = ""
            _setup_state[platform]["error"] = str(e)
            _setup_state[platform]["finished_at"] = datetime.now(timezone.utc).isoformat()
        traceback.print_exc()


def start_connect(platform: str) -> dict[str, Any]:
    """
    Start interactive connect on the poster host (opens a browser there).
    Returns immediately; poll GET /api/connections for completion.
    """
    platform = (platform or "").lower().strip()
    if platform not in ("youtube", "instagram", "tiktok"):
        return {"success": False, "error": f"Unsupported platform: {platform}"}

    if platform == "youtube" and not youtube_can_connect():
        return {
            "success": False,
            "error": (
                "client_secrets.json not found on the poster server. "
                "Add Google OAuth desktop credentials to posting_clips/ first."
            ),
        }

    with _setup_lock:
        if _setup_state[platform]["running"]:
            return {
                "success": True,
                "started": False,
                "message": f"{platform} connect already in progress — finish login in the browser on the poster host.",
                "platform": platform,
            }
        _setup_state[platform] = {
            "running": True,
            "message": "Browser login started on poster host…",
            "error": None,
            "finished_at": None,
        }

    thread = threading.Thread(target=_run_setup, args=(platform,), daemon=True, name=f"setup-{platform}")
    thread.start()

    hints = {
        "youtube": "A browser should open on the poster machine — approve YouTube upload access.",
        "instagram": "A browser should open on the poster machine — log into Instagram, then wait for auto-detect.",
        "tiktok": "A browser should open on the poster machine — log into TikTok Studio, then wait for auto-detect.",
    }
    return {
        "success": True,
        "started": True,
        "platform": platform,
        "message": hints[platform],
    }


def disconnect_platform(platform: str) -> dict[str, Any]:
    """Remove stored credentials for a platform on the poster host."""
    platform = (platform or "").lower().strip()
    cfg = load_config()
    removed: list[str] = []

    try:
        if platform == "youtube":
            _, token = _youtube_paths(cfg)
            if os.path.isfile(token):
                os.remove(token)
                removed.append(token)
            if "youtube" in cfg and "token" in cfg["youtube"]:
                del cfg["youtube"]["token"]
                save_clients_config(cfg)
                removed.append("clients.json:youtube.token")
        elif platform == "instagram":
            ig_dir, ig_file = _instagram_paths(cfg)
            if os.path.isdir(ig_dir):
                shutil.rmtree(ig_dir, ignore_errors=True)
                removed.append(ig_dir)
            if os.path.isfile(ig_file):
                os.remove(ig_file)
                removed.append(ig_file)
            if "instagram" in cfg and ("authorization_data" in cfg["instagram"] or "sessionid" in cfg["instagram"]):
                cfg["instagram"].pop("authorization_data", None)
                cfg["instagram"].pop("sessionid", None)
                save_clients_config(cfg)
                removed.append("clients.json:instagram_session")
        elif platform == "tiktok":
            tt_dir = _tiktok_path(cfg)
            if os.path.isdir(tt_dir):
                shutil.rmtree(tt_dir, ignore_errors=True)
                removed.append(tt_dir)
        else:
            return {"success": False, "error": f"Unsupported platform: {platform}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    # Drop stale config marker if present
    try:
        if os.path.isfile("config.json"):
            with open("config.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            # no structural change needed — sessions are path-based
            _ = data
    except Exception:
        pass

    return {
        "success": True,
        "platform": platform,
        "removed": removed,
        "message": f"Disconnected {platform}." if removed else f"No stored credentials found for {platform}.",
    }
