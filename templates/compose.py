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


def probe_duration_seconds(path: Path | str) -> float:
    """Return media duration in seconds (0 if unknown)."""
    src = Path(path)
    if not src.is_file():
        return 0.0
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(src),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        return max(0.0, float((proc.stdout or "").strip()))
    except ValueError:
        return 0.0


def extract_audio_track(
    video_path: Path | str,
    output_path: Path | str,
    *,
    audio_ss: float = 0.0,
    audio_duration: float | None = None,
) -> Path:
    """Extract AAC/MP3-friendly audio from a downloaded moment (or any video)."""
    src = Path(video_path)
    dest = Path(output_path)
    if not src.is_file():
        raise FileNotFoundError(f"extract-audio input missing: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y"]
    ss = max(0.0, float(audio_ss or 0.0))
    if ss > 0.01:
        cmd.extend(["-ss", f"{ss:.3f}"])
    cmd.extend(["-i", str(src)])
    if audio_duration is not None and float(audio_duration) > 0.05:
        cmd.extend(["-t", f"{float(audio_duration):.3f}"])
    cmd.extend(
        [
            "-vn",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(dest),
        ]
    )
    _run_ffmpeg(cmd, label="extract-audio")
    if not dest.is_file():
        raise RuntimeError(f"extract-audio produced no file: {dest}")
    return dest


def trim_video_start(
    video_path: Path | str,
    output_path: Path | str,
    *,
    start_sec: float,
    force_cpu: bool = False,
) -> Path:
    """Drop the first ``start_sec`` of video (+audio) so moment A/V can stay in sync."""
    src = Path(video_path)
    dest = Path(output_path)
    start = max(0.0, float(start_sec or 0.0))
    if start < 0.05:
        if src.resolve() != dest.resolve():
            import shutil

            shutil.copy2(src, dest)
        return dest
    if not src.is_file():
        raise FileNotFoundError(f"trim input missing: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _, encoder_args = detect_best_encoder(force_cpu=force_cpu)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(src),
        *encoder_args,
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    _run_ffmpeg(cmd, label="trim-start")
    if not dest.is_file():
        raise RuntimeError(f"trim-start produced no file: {dest}")
    return dest


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


def replace_body_audio(
    video_path: Path | str,
    audio_path: Path | str,
    output_path: Path | str,
    *,
    audio_ss: float = 0.0,
    audio_duration: float | None = None,
    force_cpu: bool = False,
) -> Path:
    """Legacy body-only mux. Prefer ``apply_timeline_audio`` for Process."""
    return apply_timeline_audio(
        video_path,
        audio_path,
        output_path,
        audio_ss=audio_ss,
        audio_duration=audio_duration,
        delay_sec=0.0,
        force_cpu=force_cpu,
    )


def apply_timeline_audio(
    video_path: Path | str,
    audio_path: Path | str,
    output_path: Path | str,
    *,
    audio_ss: float = 0.0,
    delay_sec: float = 0.0,
    audio_duration: float | None = None,
    force_cpu: bool = False,
) -> Path:
    """
    Replace the stitched clip's audio with a Dynamic MP3 bed placed on the
    full Editor timeline (bumper + body). Does not restart at the Dynamic cut.

    ``audio_ss`` — source offset into the MP3.
    ``delay_sec`` — silence at the start of the final clip before the MP3 begins
    (when the clip starts after bumper origin on the Editor timeline).
    """
    video = Path(video_path)
    audio = Path(audio_path)
    dest = Path(output_path)
    if not video.is_file():
        raise FileNotFoundError(f"timeline video missing: {video}")
    if not audio.is_file():
        raise FileNotFoundError(f"timeline audio missing: {audio}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    _, encoder_args = detect_best_encoder(force_cpu=force_cpu)
    ss = max(0.0, float(audio_ss or 0.0))
    delay = max(0.0, float(delay_sec or 0.0))
    delay_ms = int(round(delay * 1000.0))

    # Trim/seek the MP3 as input 1, then optionally delay so it keeps Editor timing.
    af_parts = [
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
    ]
    if delay_ms > 0:
        af_parts.append(f"adelay={delay_ms}|{delay_ms}")
    af = ",".join(af_parts)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-ss",
        f"{ss:.3f}",
    ]
    if audio_duration is not None and float(audio_duration) > 0.05:
        # Cover delay + playable span so -shortest can follow the video.
        cmd.extend(["-t", f"{float(audio_duration) + delay:.3f}"])
    cmd.extend(
        [
            "-i",
            str(audio),
            "-filter_complex",
            f"[1:a]{af}[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-shortest",
            *encoder_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    _run_ffmpeg(cmd, label="timeline-audio")
    if not dest.is_file():
        raise RuntimeError(f"timeline-audio produced no file: {dest}")
    return dest
