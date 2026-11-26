"""
Server-side access control for internal YouTube import / discovery tools.

Never trust client-supplied emails or X-Braindance-Owner-Id alone.
Callers must present a valid Supabase access token; we verify it and
check the authenticated email against the hardcoded allowlist.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import Header, HTTPException

# Hardcoded operator allowlist for YouTube import + Discover.
# Optional env INTERNAL_YOUTUBE_EMAILS can ADD emails (comma-separated), not replace.
_HARDCODED_YOUTUBE_IMPORT_EMAILS = frozenset(
    {
        "khaydenroberts22@gmail.com",
    }
)


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def youtube_import_emails() -> frozenset[str]:
    extra = os.environ.get("INTERNAL_YOUTUBE_EMAILS", "").strip()
    emails = set(_HARDCODED_YOUTUBE_IMPORT_EMAILS)
    if extra:
        for part in extra.split(","):
            cleaned = normalize_email(part)
            if cleaned:
                emails.add(cleaned)
    return frozenset(emails)


def is_youtube_import_email(email: str | None) -> bool:
    return normalize_email(email) in youtube_import_emails()


def _extract_bearer(authorization: str | None) -> str | None:
    raw = (authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        return None
    token = raw[7:].strip()
    return token or None


def verify_supabase_user(access_token: str) -> dict[str, Any]:
    """Validate JWT via Supabase Auth and return {id, email}."""
    from db import get_supabase, is_configured

    if not is_configured():
        raise HTTPException(status_code=503, detail="Auth backend is not configured.")

    try:
        sb = get_supabase()
        result = sb.auth.get_user(access_token)
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid or expired session: {exc}",
        ) from exc

    user = getattr(result, "user", None) or (result.get("user") if isinstance(result, dict) else None)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")

    user_id = getattr(user, "id", None) or (user.get("id") if isinstance(user, dict) else None)
    email = getattr(user, "email", None) or (user.get("email") if isinstance(user, dict) else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")

    return {
        "id": str(user_id),
        "email": normalize_email(str(email) if email else ""),
    }


def require_authenticated_user(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Sign in required. Missing Authorization Bearer token.",
        )
    return verify_supabase_user(token)


def require_youtube_import_user(
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """
    Hard gate for YouTube download / comment scrape / Discover / render-from-URL.
    """
    user = require_authenticated_user(authorization)
    if not is_youtube_import_email(user.get("email")):
        raise HTTPException(
            status_code=403,
            detail="YouTube import is restricted to authorized Braindance operators.",
        )
    return user
