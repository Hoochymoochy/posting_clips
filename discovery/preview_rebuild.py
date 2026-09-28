"""Re-render discovery preview MP4s without burned-in YouTube set titles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from discovery.candidates import (
    discovery_preview_path,
    get_candidate,
    list_candidates,
    update_candidate,
)
from discovery.render import render_preview


def rebuild_candidate_preview(supabase, candidate_id: str) -> dict[str, Any]:
    candidate = get_candidate(supabase, candidate_id)
    if not candidate:
        raise LookupError(f"Candidate {candidate_id} not found")

    youtube_url = (candidate.get("youtube_url") or "").strip()
    if not youtube_url:
        raise ValueError(f"Candidate {candidate_id} has no youtube_url")

    start = candidate.get("start_time") or "00:00:00"
    end = candidate.get("end_time") or start
    segment = candidate.get("segment_path")
    segment_path = Path(segment) if segment and Path(segment).is_file() else None
    preview_path = discovery_preview_path(candidate_id)

    render_preview(
        youtube_url=youtube_url,
        start=start,
        end=end,
        title=None,
        out_path=preview_path,
        segment_path=segment_path,
    )

    return update_candidate(
        supabase,
        candidate_id,
        preview_path=str(preview_path),
    )


def rebuild_awaiting_review_previews(
    supabase,
    *,
    limit: int = 50,
    status: str = "awaiting_review",
) -> dict[str, Any]:
    rows = list_candidates(supabase, status=status, limit=max(1, min(int(limit), 100)))
    rebuilt: list[str] = []
    failed: list[dict[str, str]] = []

    for row in rows:
        cid = str(row.get("id") or "")
        if not cid:
            continue
        try:
            rebuild_candidate_preview(supabase, cid)
            rebuilt.append(cid)
        except Exception as exc:
            failed.append({"id": cid, "error": str(exc)[:500]})

    return {
        "success": len(failed) == 0,
        "rebuilt": len(rebuilt),
        "failed_count": len(failed),
        "candidate_ids": rebuilt,
        "errors": failed,
    }
