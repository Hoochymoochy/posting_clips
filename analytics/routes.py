#!/usr/bin/env python3
"""FastAPI routes for live analytics pulls."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from . import db
from .config import ANALYTICS_PLATFORMS, credential_status
from .sync import pull_posts

router = APIRouter(tags=["Analytics"])

VALID_PLATFORMS = set(ANALYTICS_PLATFORMS)


def _parse_platform(platform: Optional[str]) -> str | None:
    if platform is None or not str(platform).strip():
        return None
    p = platform.strip().lower()
    if p not in VALID_PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"platform must be one of: {', '.join(sorted(VALID_PLATFORMS))}",
        )
    return p


@router.get("/api/analytics/health")
def analytics_health():
    ok, err = db.check_connection()
    return {
        "ok": ok,
        "service": "analytics",
        "database": {"ok": ok, "error": err},
        "credentials": credential_status(),
    }


@router.get("/api/analytics/posts")
@router.get("/api/posts")
def get_all_posts(
    platform: Optional[str] = Query(
        None, description="Filter: youtube | instagram"
    ),
):
    """
    Pull live analytics for every successfully published channel/post.
    Writes snapshots to clip_analytics and updates channel/clip rollups.
    TikTok is currently skipped.
    """
    if not db.is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured")
    plat = _parse_platform(platform)
    try:
        posts = pull_posts(platform=plat)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"count": len(posts), "posts": posts}


@router.get("/api/analytics/posts/{clip_id}")
@router.get("/api/posts/{clip_id}")
def get_post(
    clip_id: str,
    platform: Optional[str] = Query(
        None, description="Filter: youtube | instagram"
    ),
):
    """
    Pull live analytics for one clip (all of its published platform posts).
    """
    if not db.is_configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured")

    meta = db.get_clip_meta(clip_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Clip not found: {clip_id}")

    plat = _parse_platform(platform)
    try:
        posts = pull_posts(clip_id=clip_id, platform=plat)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not posts:
        return {
            "clip_id": clip_id,
            "title": meta.get("title"),
            "channels": [],
            "message": "No successful published channels with a post_url for this clip",
        }

    return posts[0]
