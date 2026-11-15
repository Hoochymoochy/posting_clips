"""GPU/CPU vertical clip renders via generate_clips/main.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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
        err = useful[-1] if useful else (combined.strip()[-1200] or "render failed")
        if err.startswith("RuntimeError:"):
            err = err[len("RuntimeError:") :].strip()
        raise RuntimeError(err)


def _append_title(args: list[str], title: str | None) -> None:
    """Only burn text when a real overlay line is provided (skip venue titles)."""
    cleaned = (title or "").strip()
    if cleaned:
        args.extend(["--title", cleaned])


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
    _run_main_py(render_args)
    if not out_path.is_file():
        raise RuntimeError(f"Full render produced no file: {out_path}")
    return out_path
