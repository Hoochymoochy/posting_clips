"""Locate generate_clips (GPU FFmpeg pipeline / main.py)."""

from __future__ import annotations

import os
from pathlib import Path

_POSTING_ROOT = Path(__file__).resolve().parent.parent


def generate_clips_dir() -> Path:
    """
    Directory that contains main.py (yt-dlp + FFmpeg vertical render).

    Override with GENERATE_CLIPS_DIR if the packages are not siblings.
    """
    env = os.environ.get("GENERATE_CLIPS_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    sibling = _POSTING_ROOT.parent / "generate_clips"
    if (sibling / "main.py").is_file():
        return sibling.resolve()
    raise RuntimeError(
        "generate_clips/main.py not found. Set GENERATE_CLIPS_DIR to the folder "
        "that contains main.py (GPU/FFmpeg render pipeline)."
    )


def workspace_dir() -> Path:
    """Local workspace under posting_clips for discovery segments/renders."""
    env = os.environ.get("DISCOVERY_WORKSPACE", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
    else:
        p = _POSTING_ROOT / "workspace" / "discovery"
    p.mkdir(parents=True, exist_ok=True)
    return p
