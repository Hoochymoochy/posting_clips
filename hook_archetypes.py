#!/usr/bin/env python3
"""Hook archetype definitions, stats, and epsilon-greedy selection."""

from __future__ import annotations

import os
import random
from typing import Any

# ---------------------------------------------------------------------------
# Definitions (edit freely — ids are stored on clips / candidates)
# ---------------------------------------------------------------------------

HOOK_ARCHETYPES: dict[str, dict[str, str]] = {
    "question": {
        "id": "question",
        "label": "Question hook",
        "prompt_fragment": (
            "Write as a QUESTION HOOK: open with a direct question that pulls the viewer in "
            "(about the drop, the set, or the vibe). The question should make them want to watch."
        ),
    },
    "curiosity_gap": {
        "id": "curiosity_gap",
        "label": "Curiosity gap",
        "prompt_fragment": (
            "Write as a CURIOSITY-GAP HOOK: tease the payoff without spoiling it. "
            "Hint that something unexpected or elite is about to happen — leave them hanging."
        ),
    },
    "callout_relatable": {
        "id": "callout_relatable",
        "label": "Callout / relatable",
        "prompt_fragment": (
            "Write as a CALLOUT / RELATABLE HOOK: speak to people who know this feeling "
            "('if you know, you know', scene recognition, that exact club moment)."
        ),
    },
    "bold_claim": {
        "id": "bold_claim",
        "label": "Bold claim",
        "prompt_fragment": (
            "Write as a BOLD-CLAIM HOOK: make a strong, confident opinion about this drop or set. "
            "Be declarative — no hedging."
        ),
    },
    "hype_mc": {
        "id": "hype_mc",
        "label": "Hype MC",
        "prompt_fragment": (
            "Write as a HYPE-MC HOOK: club MC / ladies-and-gentlemen energy. "
            "Fun, chaotic hype — like introducing the moment on the mic."
        ),
    },
    "moment_name": {
        "id": "moment_name",
        "label": "Moment name",
        "prompt_fragment": (
            "Write as a MOMENT-NAME HOOK: name the transition or drop as the event itself "
            "(treat it like a titled moment people will remember)."
        ),
    },
    "social_proof": {
        "id": "social_proof",
        "label": "Social proof",
        "prompt_fragment": (
            "Write as a SOCIAL-PROOF HOOK: lean on fan/crowd energy "
            "('chat went crazy', 'crowd lost it') without inventing fake metrics."
        ),
    },
    "contrast": {
        "id": "contrast",
        "label": "Contrast",
        "prompt_fragment": (
            "Write as a CONTRAST HOOK: frame a before/after or calm-to-chaos shift "
            "that makes the drop hit harder."
        ),
    },
}

ARCHETYPE_IDS: tuple[str, ...] = tuple(HOOK_ARCHETYPES.keys())


def get_archetype(archetype_id: str | None) -> dict[str, str] | None:
    if not archetype_id:
        return None
    return HOOK_ARCHETYPES.get(str(archetype_id).strip().lower())


def prompt_fragment_for(archetype_id: str | None) -> str:
    meta = get_archetype(archetype_id)
    return (meta or {}).get("prompt_fragment") or ""


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def selector_config() -> dict[str, Any]:
    strategy = (os.environ.get("HOOK_STRATEGY") or "epsilon_greedy").strip().lower()
    if strategy not in ("epsilon_greedy",):
        strategy = "epsilon_greedy"
    epsilon = max(0.0, min(1.0, _env_float("HOOK_EPSILON", 0.2)))
    min_samples = max(1, _env_int("HOOK_MIN_SAMPLES", 5))
    return {
        "strategy": strategy,
        "epsilon": epsilon,
        "min_samples": min_samples,
    }


# ---------------------------------------------------------------------------
# Stats (YouTube average_watch_percent only, non-null)
# ---------------------------------------------------------------------------

def fetch_archetype_stats(supabase=None) -> dict[str, dict[str, Any]]:
    """
    Per-archetype aggregates from clips that have a YouTube channel with
    non-null average_watch_percent.

    Returns a dict keyed by archetype id, always including every known archetype
    (count=0 when unseen).
    """
    if supabase is None:
        from db import get_supabase

        supabase = get_supabase()

    # Fetch youtube channels with matured AWP, join clip archetype via FK embed.
    result = (
        supabase.table("channels")
        .select(
            "id, views, likes, average_watch_percent, posted_at, "
            "clips!inner(id, hook_archetype, duration_seconds)"
        )
        .eq("platform", "youtube")
        .eq("status", "success")
        .not_.is_("average_watch_percent", "null")
        .execute()
    )

    buckets: dict[str, dict[str, list[float]]] = {
        aid: {"awp": [], "views": [], "likes": []} for aid in ARCHETYPE_IDS
    }

    for row in result.data or []:
        clip = row.get("clips") or {}
        if isinstance(clip, list):
            clip = clip[0] if clip else {}
        aid = (clip.get("hook_archetype") or "").strip().lower()
        if aid not in buckets:
            continue
        awp = row.get("average_watch_percent")
        if awp is None:
            continue
        try:
            buckets[aid]["awp"].append(float(awp))
        except (TypeError, ValueError):
            continue
        try:
            buckets[aid]["views"].append(float(row.get("views") or 0))
        except (TypeError, ValueError):
            buckets[aid]["views"].append(0.0)
        try:
            buckets[aid]["likes"].append(float(row.get("likes") or 0))
        except (TypeError, ValueError):
            buckets[aid]["likes"].append(0.0)

    cfg = selector_config()
    min_samples = int(cfg["min_samples"])
    out: dict[str, dict[str, Any]] = {}
    for aid in ARCHETYPE_IDS:
        awps = buckets[aid]["awp"]
        n = len(awps)
        mean_awp = round(sum(awps) / n, 2) if n else None
        views = buckets[aid]["views"]
        likes = buckets[aid]["likes"]
        out[aid] = {
            "id": aid,
            "label": HOOK_ARCHETYPES[aid]["label"],
            "count": n,
            "mean_average_watch_percent": mean_awp,
            "mean_views": round(sum(views) / n, 1) if n else None,
            "mean_likes": round(sum(likes) / n, 2) if n else None,
            "undersampled": n < min_samples,
        }
    return out


def select_archetype(
    *,
    stats: dict[str, dict[str, Any]] | None = None,
    supabase=None,
    rng: random.Random | None = None,
) -> str:
    """
    Epsilon-greedy with cold-start exploration.

    - Archetypes with count < HOOK_MIN_SAMPLES are undersampled.
    - With probability epsilon: explore (prefer undersampled; else uniform).
    - With probability 1-epsilon: exploit best mean AWP among adequately sampled;
      if none are adequately sampled, explore undersampled.
    """
    cfg = selector_config()
    epsilon = float(cfg["epsilon"])
    min_samples = int(cfg["min_samples"])
    r = rng or random

    if stats is None:
        try:
            stats = fetch_archetype_stats(supabase)
        except Exception as exc:
            print(f"  [WARN] hook archetype stats failed ({exc}); picking uniformly")
            return r.choice(list(ARCHETYPE_IDS))

    undersampled = [
        aid for aid in ARCHETYPE_IDS if int((stats.get(aid) or {}).get("count") or 0) < min_samples
    ]
    adequate = [aid for aid in ARCHETYPE_IDS if aid not in undersampled]

    def _explore() -> str:
        pool = undersampled or list(ARCHETYPE_IDS)
        return r.choice(pool)

    def _exploit() -> str:
        if not adequate:
            return _explore()
        best_aid = adequate[0]
        best_mean = float((stats.get(best_aid) or {}).get("mean_average_watch_percent") or 0.0)
        for aid in adequate[1:]:
            mean = (stats.get(aid) or {}).get("mean_average_watch_percent")
            if mean is None:
                continue
            mean_f = float(mean)
            if mean_f > best_mean:
                best_mean = mean_f
                best_aid = aid
        return best_aid

    # Cold start: if any archetype lacks samples, force exploration among them
    # unless we are in exploit mode AND have at least one adequate arm —
    # still explore when random draw hits epsilon.
    if undersampled and not adequate:
        return _explore()

    if r.random() < epsilon:
        return _explore()
    return _exploit()


def confound_summary_from_channels(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Build duration / posted_at hour & DOW distributions for reporting.
    Selector does not use these.
    """
    duration_buckets: dict[str, int] = {
        "0-15s": 0,
        "15-30s": 0,
        "30-45s": 0,
        "45-60s": 0,
        "60s+": 0,
        "unknown": 0,
    }
    hour_counts: dict[str, int] = {str(h): 0 for h in range(24)}
    dow_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    dow_counts: dict[str, int] = {d: 0 for d in dow_names}

    for row in rows:
        clip = row.get("clips") or {}
        if isinstance(clip, list):
            clip = clip[0] if clip else {}
        dur = clip.get("duration_seconds")
        try:
            d = float(dur) if dur is not None else None
        except (TypeError, ValueError):
            d = None
        if d is None:
            duration_buckets["unknown"] += 1
        elif d < 15:
            duration_buckets["0-15s"] += 1
        elif d < 30:
            duration_buckets["15-30s"] += 1
        elif d < 45:
            duration_buckets["30-45s"] += 1
        elif d <= 60:
            duration_buckets["45-60s"] += 1
        else:
            duration_buckets["60s+"] += 1

        posted = row.get("posted_at")
        if not posted:
            continue
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            hour_counts[str(dt.hour)] += 1
            dow_counts[dow_names[dt.weekday()]] += 1
        except ValueError:
            continue

    return {
        "duration_buckets": duration_buckets,
        "posted_hour_utc": hour_counts,
        "posted_dow_utc": dow_counts,
    }


def build_hook_report(supabase=None) -> dict[str, Any]:
    """Full report payload for API / CLI."""
    if supabase is None:
        from db import get_supabase

        supabase = get_supabase()

    stats = fetch_archetype_stats(supabase)
    cfg = selector_config()

    result = (
        supabase.table("channels")
        .select(
            "id, views, likes, average_watch_percent, posted_at, "
            "clips!inner(id, hook_archetype, duration_seconds)"
        )
        .eq("platform", "youtube")
        .eq("status", "success")
        .not_.is_("average_watch_percent", "null")
        .execute()
    )
    confounds = confound_summary_from_channels(list(result.data or []))

    return {
        "config": cfg,
        "archetypes": [stats[aid] for aid in ARCHETYPE_IDS],
        "confounds": confounds,
    }
