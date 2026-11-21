"""GPU/CPU vertical clip renders via generate_clips/main.py + template compose."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from discovery.paths import generate_clips_dir


def _run_main_py(args: list[str]) -> None:
    root = generate_clips_dir()
    cmd = [sys.executable, str(root / "main.py"), *args]
    print("  [render]", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdout:
        print(proc.stdout[-4000:])
    if proc.returncode != 0:
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]
        # Prefer the actual RuntimeError / ERROR line over a long traceback
        useful = [
            ln
            for ln in lines
            if ln.startswith("RuntimeError:")
            or ln.startswith("ERROR:")
            or "ERROR:" in ln
            or ln.startswith("Download failed")
        ]
        err = useful[-1] if useful else (combined.strip()[-1200:] or "render failed")
        if err.startswith("RuntimeError:"):
            err = err[len("RuntimeError:") :].strip()
        raise RuntimeError(err)


def _append_title(args: list[str], title: str | None) -> None:
    """Only burn text when a real overlay line is provided (skip venue titles)."""
    cleaned = (title or "").strip()
    if cleaned:
        args.extend(["--title", cleaned])


def _append_style(
    args: list[str],
    *,
    text_style: str | None = None,
    font: str | None = None,
    font_size: int | None = None,
    title_y: str | None = None,
    color: str | None = None,
) -> None:
    if text_style:
        args.extend(["--text-style", str(text_style)])
    if font:
        args.extend(["--font", str(font)])
    if font_size is not None:
        args.extend(["--font-size", str(int(font_size))])
    if title_y:
        args.extend(["--title-y", str(title_y)])
    if color:
        args.extend(["--color", str(color)])


def ensure_segment(
    *,
    youtube_url: str,
    start: str,
    end: str,
    segment_path: Path,
) -> Path:
    """Download/extract the raw YouTube window once for shared variant bodies."""
    segment_path.parent.mkdir(parents=True, exist_ok=True)
    if segment_path.is_file():
        return segment_path
    _run_main_py([
        "--url", youtube_url,
        "--start", start,
        "--end", end,
        "--output", str(segment_path),
        "--segment-only",
        "--max-res", "1080",
    ])
    if not segment_path.is_file():
        raise RuntimeError(f"Segment download produced no file: {segment_path}")
    return segment_path


def render_preview(
    *,
    youtube_url: str,
    start: str,
    end: str,
    title: str | None = None,
    out_path: Path,
    segment_path: Path | None = None,
) -> Path:
    """Cheap vertical preview (720p / --preview). GPU→CPU fallback inside main.py.

    Pass ``title=None`` / empty to skip burned-in text so the review UI can
    preview the hook overlay in CSS without stacking a venue title.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if segment_path is not None:
        segment_path.parent.mkdir(parents=True, exist_ok=True)

    if segment_path is not None and not segment_path.is_file():
        _run_main_py([
            "--url", youtube_url,
            "--start", start,
            "--end", end,
            "--output", str(segment_path),
            "--segment-only",
            "--max-res", "720",
        ])

    source = str(segment_path) if segment_path and segment_path.is_file() else youtube_url
    render_args = [
        "--url", source,
        "--start", "00:00:00" if source != youtube_url else start,
        "--end", "99:59:59" if source != youtube_url else end,
        "--output", str(out_path),
        "--mode", "fullscreen",
        "--max-res", "720",
        "--preview",
    ]
    _append_title(render_args, title)
    _run_main_py(render_args)
    if not out_path.is_file():
        raise RuntimeError(f"Preview render produced no file: {out_path}")
    return out_path


def render_full(
    *,
    youtube_url: str,
    start: str,
    end: str,
    title: str | None = None,
    out_path: Path,
    segment_path: Path | None = None,
    text_style: str | None = None,
    font: str | None = None,
    font_size: int | None = None,
    title_y: str | None = None,
    color: str | None = None,
) -> Path:
    """Full-quality vertical short for posting after approve.

    Pass ``title=None`` / empty for a clean render with no burned-in text
    (Manual Clip Studio). Discovery approve should pass a real hook overlay.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    source = str(segment_path) if segment_path and segment_path.is_file() else youtube_url
    render_args = [
        "--url", source,
        "--start", "00:00:00" if source != youtube_url else start,
        "--end", "99:59:59" if source != youtube_url else end,
        "--output", str(out_path),
        "--mode", "fullscreen",
        "--max-res", "1080",
    ]
    _append_title(render_args, title)
    _append_style(
        render_args,
        text_style=text_style,
        font=font,
        font_size=font_size,
        title_y=title_y,
        color=color,
    )
    _run_main_py(render_args)
    if not out_path.is_file():
        raise RuntimeError(f"Full render produced no file: {out_path}")
    return out_path


def _style_from_section(section: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if section.get("text_style"):
        out["text_style"] = str(section["text_style"])
    if section.get("font"):
        out["font"] = str(section["font"])
    if section.get("font_size") is not None:
        try:
            out["font_size"] = int(section["font_size"])
        except (TypeError, ValueError):
            pass
    if section.get("title_y"):
        out["title_y"] = str(section["title_y"])
    if section.get("color"):
        out["color"] = str(section["color"])
    return out


def render_variant_header(
    *,
    youtube_url: str,
    start: str,
    end: str,
    title: str,
    out_path: Path,
    segment_path: Path | None = None,
    config: dict[str, Any] | None = None,
) -> Path:
    """Header template: hook overlay style + optional loudnorm."""
    from templates.compose import apply_loudnorm
    from templates.loader import load_template_config

    cfg = config if config is not None else load_template_config()
    header = cfg.get("header") or {}
    style = _style_from_section(header if isinstance(header, dict) else {})

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="variant_header_") as tmp:
        raw = Path(tmp) / "header_body.mp4"
        render_full(
            youtube_url=youtube_url,
            start=start,
            end=end,
            title=title,
            out_path=raw,
            segment_path=segment_path,
            **style,
        )
        if header.get("loudnorm", True):
            apply_loudnorm(raw, out_path)
        else:
            import shutil

            shutil.copy2(raw, out_path)

    if not out_path.is_file():
        raise RuntimeError(f"Header variant produced no file: {out_path}")
    return out_path


def render_variant_intro(
    *,
    youtube_url: str,
    start: str,
    end: str,
    title: str,
    out_path: Path,
    segment_path: Path | None = None,
    config: dict[str, Any] | None = None,
    bumper_path: Path | str | None = None,
    layout: dict[str, Any] | None = None,
) -> Path:
    """Intro template: moment→body MP4/MP3, concat bumper, apply moment audio by Editor timing."""
    from templates.compose import (
        apply_loudnorm,
        apply_timeline_audio,
        concat_bumper_and_body,
        extract_audio_track,
        probe_duration_seconds,
        trim_video_start,
    )
    from templates.loader import load_template_config, resolve_bumper_path

    cfg = config if config is not None else load_template_config()
    intro = cfg.get("intro") or {}
    if not isinstance(intro, dict):
        intro = {}
    layout = layout if isinstance(layout, dict) else {}

    # Prefer bound-project typography; fall back to shared config.json intro section.
    style = _style_from_section(intro)
    project_style = layout.get("style") if isinstance(layout.get("style"), dict) else {}
    style.update({k: v for k, v in project_style.items() if v is not None and v != ""})

    bumper = Path(bumper_path) if bumper_path else resolve_bumper_path(cfg)
    if "body_title" in layout:
        body_title = bool(layout.get("body_title"))
    else:
        body_title = bool(intro.get("body_title", False))
    burn_title = (title or "").strip() if body_title else ""

    # Editor timing only — media always comes from the downloaded moment.
    timeline_audio = layout.get("timeline_audio") if isinstance(layout.get("timeline_audio"), dict) else None
    if timeline_audio is None and isinstance(layout.get("body_audio"), dict):
        timeline_audio = layout.get("body_audio")
    use_moment_audio = bool(layout.get("dynamic_from_moment", True)) and timeline_audio is not None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="variant_intro_") as tmp:
        body = Path(tmp) / "intro_body.mp4"
        body_for_concat = Path(tmp) / "intro_body_sync.mp4"
        stitched = Path(tmp) / "intro_stitched.mp4"
        with_bed = Path(tmp) / "intro_with_bed.mp4"
        moment_audio = Path(tmp) / "moment_audio.m4a"

        # Dynamic MP4 = downloaded Discover moment (segment or URL window).
        render_full(
            youtube_url=youtube_url,
            start=start,
            end=end,
            title=burn_title,
            out_path=body,
            segment_path=segment_path,
            **style,
        )

        bumper_dur = probe_duration_seconds(bumper)
        try:
            editor_delay = float((timeline_audio or {}).get("delay_sec") or 0.0)
        except (TypeError, ValueError):
            editor_delay = 0.0
        try:
            editor_ss = float((timeline_audio or {}).get("ss") or 0.0)
        except (TypeError, ValueError):
            editor_ss = 0.0
        try:
            editor_dur = (
                float(timeline_audio["duration"])
                if timeline_audio and timeline_audio.get("duration") is not None
                else None
            )
        except (TypeError, ValueError):
            editor_dur = None

        # Preroll (Editor Dynamic MP3 under bumper): play moment audio from t=0 and
        # trim body so A/V meet when the bumper ends. Otherwise sync audio to body.
        preroll = use_moment_audio and editor_delay <= 0.05
        if preroll and bumper_dur > 0.05:
            trim_video_start(body, body_for_concat, start_sec=bumper_dur)
            audio_delay = 0.0
            audio_ss = editor_ss
            print(
                f"  [templates] moment audio preroll under bumper "
                f"(trim body {bumper_dur:.2f}s for A/V sync)"
            )
        else:
            body_for_concat = body
            audio_delay = bumper_dur if use_moment_audio else editor_delay
            audio_ss = editor_ss

        # 1) Bumper + moment video.
        concat_bumper_and_body(bumper, body_for_concat, stitched)

        # 2) Dynamic MP3 = audio extracted from the same downloaded moment.
        final_video = stitched
        if use_moment_audio:
            audio_src = Path(segment_path) if segment_path and Path(segment_path).is_file() else body
            print(f"  [templates] Dynamic MP3 ← downloaded moment ({audio_src.name})")
            extract_audio_track(audio_src, moment_audio)
            print(
                f"  [templates] timeline audio ← moment "
                f"(ss={audio_ss:.2f}s, delay={audio_delay:.2f}s, "
                f"dur={editor_dur if editor_dur is not None else 'auto'})"
            )
            apply_timeline_audio(
                stitched,
                moment_audio,
                with_bed,
                audio_ss=audio_ss,
                delay_sec=audio_delay,
                audio_duration=editor_dur,
            )
            final_video = with_bed

        if intro.get("loudnorm", True):
            apply_loudnorm(final_video, out_path)
        else:
            import shutil

            shutil.copy2(final_video, out_path)

    if not out_path.is_file():
        raise RuntimeError(f"Intro variant produced no file: {out_path}")
    return out_path
