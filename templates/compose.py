"""FFmpeg helpers: loudness normalize + bumper concat for template variants."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from render.main import detect_best_encoder


def _run_ffmpeg(cmd: list[str], *, label: str) -> None:
    print(f"  [templates/{label}]", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ffmpeg failed").strip()
        raise RuntimeError(f"{label} failed: {err[-1200:]}")


def apply_loudnorm(
    input_path: Path | str,
    output_path: Path | str,
    *,
    force_cpu: bool = False,
) -> Path:
    """Re-encode audio with loudnorm; re-encode video to keep a clean MP4."""
    src = Path(input_path)
    dest = Path(output_path)
    if not src.is_file():
        raise FileNotFoundError(f"loudnorm input missing: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    _, encoder_args = detect_best_encoder(force_cpu=force_cpu)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-af",
        "loudnorm=I=-14:TP=-1.5:LRA=11",
        *encoder_args,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    _run_ffmpeg(cmd, label="loudnorm")
    if not dest.is_file():
        raise RuntimeError(f"loudnorm produced no file: {dest}")
    return dest


def concat_bumper_and_body(
    bumper_path: Path | str,
    body_path: Path | str,
    output_path: Path | str,
    *,
    force_cpu: bool = False,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> Path:
    """
    Prepend bumper to body as a 9:16 vertical short.

    Both inputs are scaled/cropped to the same canvas and audio layout, then concatenated.
    """
    bumper = Path(bumper_path)
    body = Path(body_path)
    dest = Path(output_path)
    if not bumper.is_file():
        raise FileNotFoundError(
            f"Intro bumper not found: {bumper}. "
            "Drop your branded intro at backend/templates/assets/intro.mp4"
        )
    if not body.is_file():
        raise FileNotFoundError(f"Intro body clip missing: {body}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    _, encoder_args = detect_best_encoder(force_cpu=force_cpu)
    # Scale each stream to 1080x1920, stereo 48k, then concat.
    # Missing audio on bumper → generate silence matching bumper duration via anullsrc is hard
    # in one filter; instead use optional audio maps with apad/aresample and concat.
    fc = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p[v0];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p[v1];"
        f"[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"aresample=async=1:first_pts=0[a0];"
        f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"aresample=async=1:first_pts=0[a1];"
        f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]"
    )

    with tempfile.TemporaryDirectory(prefix="tpl_concat_") as tmp:
        # If bumper has no audio, synthesize silence then retry with a silent track muxed.
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(bumper),
            "-i",
            str(body),
            "-filter_complex",
            fc,
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            *encoder_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            # Bumper may lack an audio stream — prepend silent audio then retry.
            silent_bumper = Path(tmp) / "bumper_with_audio.mp4"
            silence_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(bumper),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-shortest",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                *encoder_args,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(silent_bumper),
            ]
            _run_ffmpeg(silence_cmd, label="bumper-silence")
            cmd[3] = str(silent_bumper)
            _run_ffmpeg(cmd, label="concat")
        else:
            print(f"  [templates/concat] ok → {dest}")

    if not dest.is_file():
        raise RuntimeError(f"concat produced no file: {dest}")
    return dest
