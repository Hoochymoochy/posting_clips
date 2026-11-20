"""Resolve editor project layout for Discover Process (Dynamic audio + hook style)."""

from __future__ import annotations

import re
from typing import Any


# Editor preview canvas is ~320px wide; ffmpeg body is 1080px.
_EDITOR_PREVIEW_WIDTH = 320.0
_BODY_WIDTH = 1080.0


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").lower().replace("_", " ").replace("-", " ")).strip()


def _is_dynamic_asset(asset: dict[str, Any] | None) -> bool:
    if not isinstance(asset, dict):
        return False
    if asset.get("templateSlot") == "dynamic_clip":
        return True
    aid = str(asset.get("id") or "")
    if aid == "template-dynamic-clip":
        return True
    name = _norm_name(str(asset.get("name") or ""))
    return (
        "dynamic clip" in name
        or "render placeholder" in name
        or name.startswith("dynamic clip")
    )


def _clip_end(clip: dict[str, Any]) -> float:
    try:
        start = float(clip.get("start") or 0)
        inn = float(clip.get("inPoint") or 0)
        out = float(clip.get("outPoint") or inn)
        return start + max(0.0, out - inn)
    except (TypeError, ValueError):
        return float(clip.get("start") or 0)


def timeline_wants_body_title(timeline: dict[str, Any], slot: dict[str, float] | None) -> bool:
    """
    Burn Discover hook only when the Editor has caption clips on the Dynamic window
    (or any captions if the Dynamic slot is missing). Empty captions → no overlay.
    """
    captions = timeline.get("captions") if isinstance(timeline.get("captions"), list) else []
    usable = [
        c
        for c in captions
        if isinstance(c, dict) and str(c.get("text") or "").strip() and float(c.get("duration") or 0) > 0.05
    ]
    if not usable:
        return False
    if not slot:
        return True
    slot_start = float(slot["start"])
    slot_end = float(slot["end"])
    for cap in usable:
        start = float(cap.get("start") or 0)
        end = start + max(0.0, float(cap.get("duration") or 0))
        if end > slot_start + 0.05 and start < slot_end - 0.05:
            return True
    return False


def hook_style_from_timeline(timeline: dict[str, Any] | None) -> dict[str, Any]:
    """
    Map editor hook typography onto render_full / main.py style kwargs.

    Editor fontSize is preview-px (~320 wide); scale to 1080p body.
    """
    tl = timeline if isinstance(timeline, dict) else {}
    out: dict[str, Any] = {}

    font = str(tl.get("hookFont") or "").strip()
    if font:
        out["font"] = font

    style = str(tl.get("hookStyle") or "").strip()
    if style:
        out["text_style"] = style

    try:
        preview_size = float(tl.get("hookFontSize"))
        if preview_size > 0:
            out["font_size"] = max(18, int(round(preview_size * (_BODY_WIDTH / _EDITOR_PREVIEW_WIDTH))))
    except (TypeError, ValueError):
        pass

    try:
        text_y = float(tl.get("hookTextY"))
        if 0 < text_y <= 100:
            out["title_y"] = f"h*{text_y / 100.0:.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        pass

    return out


def resolve_dynamic_slot(timeline: dict[str, Any], assets: list[dict[str, Any]]) -> dict[str, float] | None:
    """Return Dynamic VIDEO slot {start, end, duration} on the editor timeline."""
    by_id = {str(a.get("id")): a for a in assets if isinstance(a, dict) and a.get("id")}
    clips = timeline.get("clips") if isinstance(timeline.get("clips"), list) else []
    dynamic_videos: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        aid = str(clip.get("assetId") or "")
        if aid == "template-dynamic-clip":
            dynamic_videos.append(clip)
            continue
        asset = by_id.get(aid)
        if asset and asset.get("kind") == "video" and _is_dynamic_asset(asset):
            dynamic_videos.append(clip)
    if not dynamic_videos:
        return None
    start = min(float(c.get("start") or 0) for c in dynamic_videos)
    end = max(_clip_end(c) for c in dynamic_videos)
    dur = max(0.05, end - start)
    return {"start": start, "end": end, "duration": dur}


def resolve_bumper_origin(timeline: dict[str, Any], assets: list[dict[str, Any]]) -> float:
    """Editor timeline time that maps to t=0 of the baked intro bumper."""
    by_id = {str(a.get("id")): a for a in assets if isinstance(a, dict) and a.get("id")}
    clips = timeline.get("clips") if isinstance(timeline.get("clips"), list) else []
    starts: list[float] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        asset = by_id.get(str(clip.get("assetId") or ""))
        if not asset or asset.get("kind") != "video":
            continue
        if _is_dynamic_asset(asset) or str(clip.get("assetId")) == "template-dynamic-clip":
            continue
        starts.append(float(clip.get("start") or 0))
    return min(starts) if starts else 0.0


def resolve_timeline_audio(
    project_id: str,
    timeline: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    bumper_origin: float | None = None,
    slot: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """
    Resolve Dynamic audio *timing* from the Editor timeline.

    Process fills the audio from the downloaded Discover moment (not the silent
    placeholder strip). Returns placement relative to baked bumper t=0:

      source    — always \"moment\" (downloaded segment)
      delay_sec — when moment audio starts on the final clip
      ss        — offset into the moment file
      duration  — Editor clip length (Process trims to video)
    """
    _ = project_id  # timing only; media comes from the downloaded moment
    origin = float(bumper_origin) if bumper_origin is not None else resolve_bumper_origin(timeline, assets)
    slot = slot or resolve_dynamic_slot(timeline, assets)
    by_id = {str(a.get("id")): a for a in assets if isinstance(a, dict) and a.get("id")}
    clips = timeline.get("clips") if isinstance(timeline.get("clips"), list) else []

    best: dict[str, Any] | None = None
    best_len = 0.0

    for clip in clips:
        if not isinstance(clip, dict):
            continue
        asset = by_id.get(str(clip.get("assetId") or ""))
        # Dynamic audio lane clip = timing marker (placeholder MP3 content ignored).
        if not asset or asset.get("kind") != "audio" or not _is_dynamic_asset(asset):
            continue

        c_start = float(clip.get("start") or 0)
        c_end = _clip_end(clip)
        try:
            inn = float(clip.get("inPoint") or 0)
        except (TypeError, ValueError):
            inn = 0.0

        play_start = max(c_start, origin)
        if c_end - play_start < 0.05:
            continue

        delay_sec = max(0.0, play_start - origin)
        ss = max(0.0, inn + (play_start - c_start))
        duration = max(0.05, c_end - play_start)
        candidate = {
            "source": "moment",
            "ss": ss,
            "delay_sec": delay_sec,
            "duration": duration,
            "timeline_start": c_start,
            "asset_id": asset.get("id"),
            "name": asset.get("name"),
        }
        if duration > best_len:
            best_len = duration
            best = candidate

    if best:
        return best

    # No Dynamic audio clip — still use moment audio, synced to the Dynamic video slot.
    if slot:
        delay = max(0.0, float(slot["start"]) - origin)
        return {
            "source": "moment",
            "ss": 0.0,
            "delay_sec": delay,
            "duration": float(slot["duration"]),
            "timeline_start": float(slot["start"]),
            "asset_id": None,
            "name": "moment",
        }
    return None


# Back-compat alias — old body-only path; prefer resolve_timeline_audio.
def resolve_body_audio(
    project_id: str,
    timeline: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    slot: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    return resolve_timeline_audio(project_id, timeline, assets, slot=slot)


def process_layout_for_template(template_row: dict[str, Any]) -> dict[str, Any]:
    """
    Build Process-time layout from the template's bound editor project.

    Empty dict when unbound / unloadable — caller falls back to config.json only.
    """
    project_id = str(template_row.get("project_id") or "").strip()
    if not project_id:
        return {}

    try:
        from projects_store import load_project

        project = load_project(project_id)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"  [templates/layout] project {project_id} unavailable: {exc}")
        return {}

    timeline = project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    assets = project.get("assets") if isinstance(project.get("assets"), list) else []
    slot = resolve_dynamic_slot(timeline, assets)
    style = hook_style_from_timeline(timeline)
    bumper_origin = resolve_bumper_origin(timeline, assets)
    timeline_audio = resolve_timeline_audio(
        project_id, timeline, assets, bumper_origin=bumper_origin, slot=slot
    )

    layout: dict[str, Any] = {
        "project_id": project_id,
        # Only burn Discover hook when Editor has captions on the Dynamic window.
        "body_title": timeline_wants_body_title(timeline, slot),
        "style": style,
        "bumper_origin": bumper_origin,
        # Dynamic video + audio always come from the downloaded Discover moment.
        "dynamic_from_moment": True,
    }
    if timeline_audio:
        layout["timeline_audio"] = timeline_audio
    if slot:
        layout["dynamic_slot"] = slot
    return layout
