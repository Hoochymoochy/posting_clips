"""
Per-creator publishing credential store (official APIs only).

Replaces the host-global clients.json model for the public Studio product.
Browser automation credentials are intentionally not stored here.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")

PLATFORMS = ("tiktok", "instagram", "youtube")


def get_credentials_dir() -> Path:
    raw = os.environ.get("CREATOR_CREDENTIALS_DIR", "creator_credentials").strip()
    p = Path(raw).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(owner_id: str) -> Path:
    oid = (owner_id or "").strip()
    if not _SAFE.fullmatch(oid):
        raise ValueError("Invalid owner id")
    return get_credentials_dir() / f"{oid}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default(owner_id: str) -> dict[str, Any]:
    return {
        "owner_id": owner_id,
        "updated_at": _now(),
        "connections": {
            "tiktok": {
                "status": "unavailable",
                "reason": "TikTok Content Posting API access not yet approved for this app.",
                "connected": False,
            },
            "instagram": {
                "status": "unavailable",
                "reason": "Official Instagram publishing integration coming soon.",
                "connected": False,
            },
            "youtube": {
                "status": "unavailable",
                "reason": "YouTube Data API publishing for creators coming soon.",
                "connected": False,
            },
        },
    }


def load_credentials(owner_id: str) -> dict[str, Any]:
    path = _path(owner_id)
    if not path.is_file():
        return _default(owner_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return _default(owner_id)
    base = _default(owner_id)
    conns = data.get("connections") if isinstance(data.get("connections"), dict) else {}
    for platform in PLATFORMS:
        if platform in conns and isinstance(conns[platform], dict):
            base["connections"][platform] = {
                **base["connections"][platform],
                **conns[platform],
            }
    base["updated_at"] = data.get("updated_at") or base["updated_at"]
    return base


def public_connections(owner_id: str | None) -> dict[str, Any]:
    """Safe status payload — never includes secrets."""
    if not owner_id:
        return {
            "authenticated": False,
            "connections": _default("anonymous")["connections"],
            "note": "Sign in to manage per-creator publishing connections.",
        }
    data = load_credentials(owner_id)
    safe: dict[str, Any] = {}
    for platform, row in (data.get("connections") or {}).items():
        if not isinstance(row, dict):
            continue
        safe[platform] = {
            "status": row.get("status") or "unavailable",
            "connected": bool(row.get("connected")),
            "reason": row.get("reason"),
            "display_name": row.get("display_name"),
        }
    return {
        "authenticated": True,
        "owner_id": owner_id,
        "connections": safe,
        "updated_at": data.get("updated_at"),
    }
