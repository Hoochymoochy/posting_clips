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
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout[-4000:])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "render failed")[-2000:]
        raise RuntimeError(err)


def render_preview(
    *,
    youtube_url: str,
    start: str,
    end: str,
    title: str,
    out_path: Path,
    segment_path: Path | None = None,
) -> Path:
    """Cheap vertical preview (720p / --preview). GPU→CPU fallback inside main.py."""
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
        "--title", title or "DJ Clip",
        "--output", str(out_path),
        "--mode", "fullscreen",
        "--max-res", "720",
        "--preview",
    ]
    _run_main_py(render_args)
    if not out_path.is_file():
        raise RuntimeError(f"Preview render produced no file: {out_path}")
    return out_path


def render_full(
    *,
    youtube_url: str,
    start: str,
    end: str,
    title: str,
    out_path: Path,
    segment_path: Path | None = None,
) -> Path:
    """Full-quality vertical short for posting after approve."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    source = str(segment_path) if segment_path and segment_path.is_file() else youtube_url
    render_args = [
        "--url", source,
        "--start", "00:00:00" if source != youtube_url else start,
        "--end", "99:59:59" if source != youtube_url else end,
        "--title", title or "DJ Clip",
        "--output", str(out_path),
        "--mode", "fullscreen",
        "--max-res", "1080",
    ]
    _run_main_py(render_args)
    if not out_path.is_file():
        raise RuntimeError(f"Full render produced no file: {out_path}")
    return out_path
