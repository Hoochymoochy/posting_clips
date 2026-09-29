"""Re-render discovery preview MP4s without burned-in YouTube set titles.

Run on the GPU host (avoids nginx 504s from the HTTP endpoint):

  cd /path/to/posting_clips
  python -m discovery.preview_rebuild --limit 50

Optional:
  python -m discovery.preview_rebuild --limit 10
  python -m discovery.preview_rebuild --id <candidate-uuid>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# Allow `python -m discovery.preview_rebuild` from posting_clips/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

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

    print(f"  [rebuild] {candidate_id}  {start}→{end}  title=None")
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

    print(f"[rebuild] {len(rows)} candidate(s) with status={status!r}")
    for i, row in enumerate(rows, start=1):
        cid = str(row.get("id") or "")
        if not cid:
            continue
        print(f"[{i}/{len(rows)}] {cid}")
        try:
            rebuild_candidate_preview(supabase, cid)
            rebuilt.append(cid)
            print(f"  [ok] {cid}")
        except Exception as exc:
            msg = str(exc)[:500]
            failed.append({"id": cid, "error": msg})
            print(f"  [fail] {cid}: {msg[:200]}")

    return {
        "success": len(failed) == 0,
        "rebuilt": len(rebuilt),
        "failed_count": len(failed),
        "candidate_ids": rebuilt,
        "errors": failed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-render discovery previews without burned-in set titles (run on GPU host)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max awaiting_review candidates to rebuild (default 50).",
    )
    parser.add_argument(
        "--status",
        default="awaiting_review",
        help="Candidate status filter (default awaiting_review).",
    )
    parser.add_argument(
        "--id",
        dest="candidate_id",
        default="",
        help="Rebuild a single candidate by UUID instead of a batch.",
    )
    args = parser.parse_args()

    from db import get_supabase, is_configured

    if not is_configured():
        print("Supabase is not configured (SUPABASE_URL / key in .env).", file=sys.stderr)
        sys.exit(1)

    sb = get_supabase()

    if args.candidate_id.strip():
        cid = args.candidate_id.strip()
        try:
            rebuild_candidate_preview(sb, cid)
        except Exception as exc:
            print(f"Failed: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Done: rebuilt {cid}")
        return

    result = rebuild_awaiting_review_previews(
        sb,
        limit=args.limit,
        status=(args.status or "awaiting_review").strip(),
    )
    print(
        f"Done: rebuilt={result['rebuilt']} failed={result['failed_count']}"
    )
    if result["errors"]:
        for err in result["errors"]:
            print(f"  - {err['id']}: {err['error'][:160]}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
