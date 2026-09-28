#!/usr/bin/env python3
"""
main.py ΓÇö Turn a YouTube clip or local video into a vertical "short" with a title overlay.

Requirements (install once):
    pip install -U yt-dlp

    FFmpeg must be installed:
      Windows: winget install Gyan.FFmpeg.Essentials   (or choco install ffmpeg)
      macOS:   brew install ffmpeg
      Linux:   sudo apt install ffmpeg

Example (PowerShell on Windows):
    python main.py `
        --url "https://www.youtube.com/watch?v=XXXXXXXX" `
        --start 00:01:23 `
        --end 00:01:53 `
        --title "DJ SNAKE" `
        --output snake_clip.mp4

Example (Bash on macOS / Linux):
    python main.py \\
        --url "https://www.youtube.com/watch?v=XXXXXXXX" \\
        --start 00:01:23 \\
        --end 00:01:53 \\
        --title "DJ SNAKE" \\
        --output snake_clip.mp4
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse


def ensure_environment():
    """Ensure PATH includes installed tools on Windows (e.g. from winget, pip, or chocolatey)."""
    if os.name != "nt":
        return

    additional_paths = []

    # 1. Check Windows Registry for updated User and System PATH (picks up recently installed tools without shell restart)
    try:
        import winreg
        for root, subkey in [
            (winreg.HKEY_CURRENT_USER, r"Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ]:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    val, _ = winreg.QueryValueEx(key, "Path")
                    additional_paths.extend(val.split(";"))
            except Exception:
                pass
    except ImportError:
        pass

    # 2. Check Python's Scripts directory
    scripts_dir = os.path.join(sys.prefix, "Scripts")
    if os.path.isdir(scripts_dir):
        additional_paths.append(scripts_dir)

    # 3. Check common WinGet package directories for ffmpeg if not on PATH
    winget_pkg_root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_pkg_root):
        matches = glob.glob(os.path.join(winget_pkg_root, "**", "ffmpeg.exe"), recursive=True)
        for match in matches:
            additional_paths.append(os.path.dirname(match))

    # Merge unique paths into os.environ["PATH"]
    current_paths = [p.rstrip(r"\/") for p in os.environ.get("PATH", "").split(";") if p.strip()]
    current_lower = {p.lower() for p in current_paths}

    for path_entry in additional_paths:
        p = path_entry.strip().rstrip(r"\/")
        if p and os.path.isdir(p) and p.lower() not in current_lower:
            current_paths.append(p)
            current_lower.add(p.lower())

    os.environ["PATH"] = ";".join(current_paths)


FONT_PRESETS = {
    "impact": "impact.ttf",
    "bahnschrift": "bahnschrift.ttf",
    "gothic": "GOTHICB.TTF",
    "segoe": "segoeuib.ttf",
    "arial": "arialbd.ttf",
    "trebuchet": "trebucbd.ttf",
    "verdana": "verdanab.ttf",
}


def get_default_font() -> str:
    """Detect operating system and return the path to a high-quality default font."""
    windir = os.environ.get("WINDIR", r"C:\Windows")

    if sys.platform == "win32" or os.name == "nt":
        # Prefer Impact or Bahnschrift on Windows for bold, modern DJ titles
        candidates = [
            os.path.join(windir, "Fonts", "impact.ttf"),      # Impact (bold, punchy for DJ titles)
            os.path.join(windir, "Fonts", "bahnschrift.ttf"), # Bahnschrift (clean modern EDM style)
            os.path.join(windir, "Fonts", "arialbd.ttf"),     # Arial Bold
            os.path.join(windir, "Fonts", "segoeuib.ttf"),    # Segoe UI Bold
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/System/Library/Fonts/Supplemental/Impact.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    else:  # Linux / BSD
        candidates = [
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]

    for font_path in candidates:
        if os.path.isfile(font_path):
            return font_path

    return candidates[0]


def resolve_font(font_input: str) -> str:
    """Resolve a font name, preset name, or file path to an absolute font path."""
    if not font_input:
        return get_default_font()

    # 1. If it's already an existing file path, use it directly
    if os.path.isfile(font_input):
        return font_input

    # 2. Check font preset keywords
    key = font_input.strip().lower()
    windir = os.environ.get("WINDIR", r"C:\Windows")
    fonts_dir = os.path.join(windir, "Fonts")

    if key in FONT_PRESETS:
        preset_file = os.path.join(fonts_dir, FONT_PRESETS[key])
        if os.path.isfile(preset_file):
            return preset_file

    # 3. Direct filename match in Fonts folder
    direct_match = os.path.join(fonts_dir, font_input)
    if os.path.isfile(direct_match):
        return direct_match
    direct_ttf = os.path.join(fonts_dir, f"{font_input}.ttf")
    if os.path.isfile(direct_ttf):
        return direct_ttf

    # 4. Search Fonts directory case-insensitively
    if os.path.isdir(fonts_dir):
        for f in os.listdir(fonts_dir):
            if f.lower().startswith(key) and f.lower().endswith((".ttf", ".otf")):
                return os.path.join(fonts_dir, f)

    # 5. Fallback
    return get_default_font()


def get_text_filter(
    font_escaped: str,
    title_file_escaped: str,
    text_style: str = "stroke",
    scale: float = 1.0,
    y_pos: str = "h*0.25",
    font_size: int = 52,
) -> str:
    """Build FFmpeg drawtext filter string according to chosen style."""
    fs = int(font_size * scale)
    bw = max(2, int(fs * 0.07))
    pad = max(4, int(fs * 0.25))
    s = text_style.lower()

    if s in ("stroke", "outline"):
        # Thick black outline + shadow (viral TikTok/Reels look without background box)
        return (
            f"drawtext=fontfile='{font_escaped}':textfile='{title_file_escaped}':expansion=none:"
            f"fontsize={fs}:fontcolor=white:borderw={bw}:bordercolor=black:"
            f"shadowcolor=black@0.8:shadowx={max(2, bw - 1)}:shadowy={max(2, bw - 1)}:x=(w-text_w)/2:y={y_pos}"
        )
    elif s == "neon":
        # Cyan neon glow with black outline (festival/rave aesthetic)
        return (
            f"drawtext=fontfile='{font_escaped}':textfile='{title_file_escaped}':expansion=none:"
            f"fontsize={fs}:fontcolor=0x00FFFF:borderw={bw}:bordercolor=black:"
            f"shadowcolor=0x0088FF@0.9:shadowx=2:shadowy=2:x=(w-text_w)/2:y={y_pos}"
        )
    elif s == "yellow":
        # High-contrast punchy yellow with black outline
        return (
            f"drawtext=fontfile='{font_escaped}':textfile='{title_file_escaped}':expansion=none:"
            f"fontsize={fs}:fontcolor=0xFFEE00:borderw={bw}:bordercolor=black:"
            f"shadowcolor=black@0.8:shadowx={max(2, bw - 1)}:shadowy={max(2, bw - 1)}:x=(w-text_w)/2:y={y_pos}"
        )
    elif s == "minimal":
        # Clean white text with drop shadow
        return (
            f"drawtext=fontfile='{font_escaped}':textfile='{title_file_escaped}':expansion=none:"
            f"fontsize={fs}:fontcolor=white:shadowcolor=black@0.9:shadowx=3:shadowy=3:x=(w-text_w)/2:y={y_pos}"
        )
    else:  # "box" / "pill"
        return (
            f"drawtext=fontfile='{font_escaped}':textfile='{title_file_escaped}':expansion=none:"
            f"fontsize={fs}:fontcolor=white:x=(w-text_w)/2:y={y_pos}:box=1:boxcolor=black@0.55:boxborderw={pad}"
        )


def get_color_filter(color_style: str = "none") -> str:
    """Return video color grading filter (e.g. vibrant rave, warm, cool)."""
    c = color_style.lower()
    if c in ("vibrant", "rave"):
        return ",eq=saturation=1.25:contrast=1.1"
    elif c == "warm":
        return ",colorbalance=rs=0.1:gs=0.0:bs=-0.1"
    elif c == "cool":
        return ",colorbalance=rs=-0.1:gs=0.0:bs=0.1"
    return ""


def escape_ffmpeg_path(path: str) -> str:
    """Normalize and escape a filesystem path for FFmpeg filtergraphs.

    On Windows, FFmpeg filter syntax treats ':' as an option separator and '\\' as an escape character.
    Drive letter colons (e.g. 'C:') must be escaped ('C\\:'), and slashes converted to forward slashes.
    """
    normalized = os.path.abspath(path).replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[0] + r"\:" + normalized[2:]
    return normalized


def check_deps():
    """Verify that required external tools exist."""
    ensure_environment()
    missing = []
    for tool in ("yt-dlp", "ffmpeg"):
        if shutil.which(tool) is None:
            missing.append(tool)

    if missing:
        msg = [f"Error: Missing required tool(s): {', '.join(missing)}."]
        if "ffmpeg" in missing:
            if os.name == "nt":
                msg.append("  Install FFmpeg on Windows via PowerShell: winget install Gyan.FFmpeg.Essentials")
            elif sys.platform == "darwin":
                msg.append("  Install FFmpeg on macOS: brew install ffmpeg")
            else:
                msg.append("  Install FFmpeg on Linux: sudo apt install ffmpeg")
        if "yt-dlp" in missing:
            msg.append("  Install yt-dlp: pip install -U yt-dlp")
        sys.exit("\n".join(msg))


def clean_youtube_url(url: str) -> str:
    """Strip playlist and tracking parameters from YouTube watch URLs to prevent downloading full playlists."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or "").lower()
        path = parsed.path or ""
        # Fix common typo: /cwatch ΓåÆ /watch
        if path == "/cwatch":
            parsed = parsed._replace(path="/watch")
            path = "/watch"
        if ("youtube.com" in host and path == "/watch") or "youtu.be" in host:
            qs = urllib.parse.parse_qs(parsed.query)
            if "v" in qs:
                clean_query = urllib.parse.urlencode({"v": qs["v"][0]})
                return urllib.parse.urlunparse(parsed._replace(query=clean_query))
            if "youtu.be" in host:
                vid = path.lstrip("/").split("/")[0]
                if vid:
                    return f"https://www.youtube.com/watch?v={vid}"
    except Exception:
        pass
    return url


def detect_best_encoder(force_cpu: bool = False, *, preview: bool = False) -> tuple[str, list[str]]:
    """Detect available GPU hardware encoder and return encoder name and CLI arguments.

    When ``preview`` is True, use faster / lower-quality settings suitable for
    discovery review clips (full render still uses the normal quality path).
    """
    if force_cpu:
        if preview:
            return "libx264 (CPU preview)", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
        return "libx264 (CPU)", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]

    # 1. Native NVIDIA NVENC (supported on driver 610+)
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True
        )
        if res.returncode == 0:
            if preview:
                return "NVIDIA NVENC preview (h264_nvenc)", ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "28"]
            return "NVIDIA NVENC (h264_nvenc)", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20"]
    except Exception:
        pass

    # 2. Windows Media Foundation (DirectX hardware encoder using your NVIDIA GPU)
    if os.name == "nt":
        try:
            res = subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=1", "-c:v", "h264_mf", "-f", "null", "-"],
                capture_output=True, text=True
            )
            if res.returncode == 0:
                if preview:
                    return "GPU / MediaFoundation preview (h264_mf)", ["-c:v", "h264_mf", "-b:v", "4M"]
                return "GPU / MediaFoundation (h264_mf)", ["-c:v", "h264_mf", "-b:v", "8M"]
        except Exception:
            pass

    # 3. Intel QuickSync Video
    try:
        res = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=1", "-c:v", "h264_qsv", "-f", "null", "-"],
            capture_output=True, text=True
        )
        if res.returncode == 0:
            if preview:
                return "Intel QuickSync preview (h264_qsv)", ["-c:v", "h264_qsv", "-global_quality", "28"]
            return "Intel QuickSync (h264_qsv)", ["-c:v", "h264_qsv", "-global_quality", "20"]
    except Exception:
        pass

    # 4. Fallback to CPU libx264
    if preview:
        return "CPU preview (libx264)", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
    return "CPU (libx264)", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def download_clip(url: str, start: str, end: str, workdir: str, max_res: str = "1080", audio_only: bool = False) -> str:
    """Download only the requested section of the video or audio with yt-dlp or trim a local file."""
    # Support using a local video/audio file directly
    if os.path.isfile(url):
        ext = "m4a" if audio_only else "mp4"
        out_clip = os.path.join(workdir, f"local_clip.{ext}")
        cmd = [
            "ffmpeg", "-y",
            "-ss", start,
            "-to", end,
            "-i", url,
        ]
        if audio_only:
            cmd.extend(["-vn", "-c:a", "copy", out_clip])
        else:
            cmd.extend(["-c:v", "libx264", "-c:a", "aac", out_clip])
        print("Extracting local clip:", " ".join(cmd))
        subprocess.run(cmd, check=True)
        return out_clip

    # Sanitize YouTube URL so playlist parameters don't cause yt-dlp to iterate hundreds of videos
    target_url = clean_youtube_url(url)
    out_template = os.path.join(workdir, "raw.%(ext)s")
    section = f"*{start}-{end}"

    cmd = [
        "yt-dlp",
        "--no-playlist",
    ]

    # YouTube blocks datacenter IPs ("Sign in to confirm you're not a bot").
    # When a cookies.txt from a logged-in browser is available, use it.
    cookies_file = os.environ.get("YT_COOKIES_FILE", "").strip()
    if cookies_file and os.path.isfile(cookies_file):
        cmd.extend(["--cookies", cookies_file])

    # Use node or deno if installed to solve YouTube JavaScript challenges and suppress runtime warnings
    if shutil.which("deno"):
        cmd.extend(["--js-runtimes", "deno", "--remote-components", "ejs:github"])
    elif shutil.which("node"):
        cmd.extend(["--js-runtimes", "node", "--remote-components", "ejs:github"])

    # Explicitly pass ffmpeg location so yt-dlp has it for partial section cuts
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        cmd.extend(["--ffmpeg-location", os.path.dirname(ffmpeg_path)])

    if audio_only:
        # Download best audio stream only (fastest download, skips video completely)
        format_selector = "ba[ext=m4a]/ba"
    elif max_res and max_res.lower() != "max":
        format_selector = f"bv*[height<={max_res}][ext=mp4]+ba[ext=m4a]/bv*[height<={max_res}]+ba/b[height<={max_res}]/bv*+ba/b"
    else:
        format_selector = "bv*[ext=mp4]+ba[ext=m4a]/mp4/bv*+ba/b"

    cmd.extend([
        "-f", format_selector,
        "--download-sections", section,
        "--force-keyframes-at-cuts",
        "-o", out_template,
        target_url,
    ])
    print("Downloading clip:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    # Find whatever file yt-dlp produced
    for f in os.listdir(workdir):
        if f.startswith("raw."):
            return os.path.join(workdir, f)
    sys.exit("Download failed: no output file found.")


def extract_audio(input_path: str, output_path: str, audio_format: str = "mp3", title: str = ""):
    """Extract audio from the downloaded clip into a standalone audio file (mp3, wav, m4a, flac)."""
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vn"]
    if title:
        cmd.extend(["-metadata", f"title={title}"])

    fmt = audio_format.lower()
    if fmt == "mp3":
        cmd.extend(["-c:a", "libmp3lame", "-q:a", "0", output_path])
    elif fmt == "wav":
        cmd.extend(["-c:a", "pcm_s16le", output_path])
    elif fmt in ("m4a", "aac"):
        cmd.extend(["-c:a", "aac", "-b:a", "320k", output_path])
    elif fmt == "flac":
        cmd.extend(["-c:a", "flac", output_path])
    else:
        cmd.extend(["-c:a", "libmp3lame", "-q:a", "0", output_path])

    print(f"Extracting {fmt.upper()} audio:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def build_short(
    input_path: str,
    title: str,
    output_path: str,
    font: str,
    mode: str,
    workdir: str,
    text_style: str = "stroke",
    color_style: str = "none",
    font_size: int = 52,
    title_y: str = "h*0.25",
    force_cpu: bool = False,
    preview: bool = False,
):
    """Reformat video to full screen vertical (default), blurred bars, or original landscape with GPU acceleration."""
    resolved_font = resolve_font(font)
    font_escaped = escape_ffmpeg_path(resolved_font)
    encoder_name, encoder_args = detect_best_encoder(force_cpu=force_cpu, preview=preview)
    print(f"Using video encoder: {encoder_name}")
    print(f"Layout mode: {mode} | Text style: {text_style} | Font size: {font_size} | Y-pos: {title_y} | Color: {color_style}")

    # Prepare title text if provided
    title_drawtext = ""
    if title.strip():
        title_file = os.path.join(workdir, "title.txt")
        with open(title_file, "w", encoding="utf-8") as f:
            f.write(title)
        title_file_escaped = escape_ffmpeg_path(title_file)

    color_vf = get_color_filter(color_style)

    if mode in ("landscape", "original"):
        # Full-screen widescreen / 16:9 (original aspect ratio, no vertical cropping)
        vf_parts = []
        if title.strip():
            vf_parts.append(get_text_filter(font_escaped, title_file_escaped, text_style=text_style, font_size=font_size, y_pos="h*0.12"))
        if color_vf:
            vf_parts.append(color_vf.lstrip(","))
        vf_str = ",".join(vf_parts) if vf_parts else "null"
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", vf_str,
            *encoder_args,
            "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]
    elif mode == "blur":
        # Blurred background fill: smooth edge-to-edge blur top and bottom with centered video
        td_str = f",{get_text_filter(font_escaped, title_file_escaped, text_style=text_style, font_size=font_size, y_pos=title_y)}" if title.strip() else ""
        filter_complex = (
            "[0:v]scale=270:480:force_original_aspect_ratio=increase,"
            "crop=270:480,boxblur=5:2,scale=1080:1920[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{color_vf}{td_str}[out]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-filter_complex", filter_complex,
            "-map", "[out]", "-map", "0:a?",
            *encoder_args,
            "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]
    else:
        # Default: Full-screen vertical 9:16 (zoom/crop to fill entire canvas, no bars)
        # Preview uses 720x1280 for faster discovery review encodes.
        out_w, out_h = (720, 1280) if preview else (1080, 1920)
        td_str = f",{get_text_filter(font_escaped, title_file_escaped, text_style=text_style, font_size=font_size, y_pos=title_y)}" if title.strip() else ""
        vf = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h}{color_vf}{td_str}"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vf", vf,
            *encoder_args,
            "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]

    print("Rendering video:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def build_comparison(
    input_path: str,
    title: str,
    output_path: str,
    font: str,
    text_style: str = "stroke",
    color_style: str = "none",
    font_size: int = 52,
    title_y: str = "h*0.25",
    workdir: str = "",
    force_cpu: bool = False,
):
    """Build side-by-side comparison video showing fullscreen vs blurred background modes."""
    resolved_font = resolve_font(font)
    font_escaped = escape_ffmpeg_path(resolved_font)
    encoder_name, encoder_args = detect_best_encoder(force_cpu=force_cpu)
    color_vf = get_color_filter(color_style)

    comp_fs = max(24, int(font_size * 0.65))
    title_filter = ""
    if title.strip():
        title_file = os.path.join(workdir, "title.txt")
        with open(title_file, "w", encoding="utf-8") as f:
            f.write(title)
        title_file_escaped = escape_ffmpeg_path(title_file)
        title_filter = f",{get_text_filter(font_escaped, title_file_escaped, text_style=text_style, font_size=comp_fs, y_pos=title_y)}"

    left_badge = f",drawtext=fontfile='{font_escaped}':text='FULL SCREEN':fontsize=32:fontcolor=white:borderw=3:bordercolor=black:shadowcolor=black@0.8:shadowx=2:shadowy=2:x=(w-text_w)/2:y=40"
    right_badge = f",drawtext=fontfile='{font_escaped}':text='BLURRED BARS':fontsize=32:fontcolor=white:borderw=3:bordercolor=black:shadowcolor=black@0.8:shadowx=2:shadowy=2:x=(w-text_w)/2:y=40"

    fc = (
        f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920{color_vf}{left_badge}{title_filter},scale=608:1080[v1];"
        f"[0:v]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=5:2,scale=1080:1920[bg];"
        f"[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2{color_vf}{right_badge}{title_filter},scale=608:1080[v2];"
        f"[v1][v2]hstack=inputs=2,pad=1920:1080:(1920-1216)/2:0:black[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a?",
        *encoder_args,
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]
    print(f"Using video encoder: {encoder_name}")
    print(f"Rendering side-by-side comparison: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser(
        description="Clip a YouTube video or local video into vertical shorts (fullscreen/blur) with custom styles and GPU acceleration."
    )
    p.add_argument("--url", required=True, help="YouTube video URL or path to a local video file")
    p.add_argument("--start", required=True, help="Clip start time, e.g. 00:01:23")
    p.add_argument("--end", required=True, help="Clip end time, e.g. 00:01:53")
    p.add_argument("--title", default="", help="Title text to overlay (and save to audio tags), e.g. a DJ name")
    p.add_argument("--output", default="short.mp4", help="Output video filename (default: short.mp4)")
    p.add_argument(
        "--font",
        default="impact",
        help="Font preset ('impact', 'bahnschrift', 'gothic', 'segoe', 'arial', 'trebuchet', 'verdana') or path to .ttf/.otf file (default: impact).",
    )
    p.add_argument(
        "--font-size",
        type=int,
        default=52,
        help="Title font size in pixels (default: 52, sized to fit ~2/4 of the screen width in the middle).",
    )
    p.add_argument(
        "--title-y",
        default="h*0.25",
        help="Vertical position of title overlay (default: 'h*0.25', placed 1/4 of the height down from the top).",
    )
    p.add_argument(
        "--text-style",
        default="stroke",
        choices=["stroke", "box", "neon", "yellow", "minimal"],
        help="Title text style: 'stroke' (clean outline & shadow - TikTok/Reels style), 'box' (semi-transparent dark bar), 'neon' (rave cyan glow), 'yellow' (high-contrast yellow), 'minimal' (clean drop shadow).",
    )
    p.add_argument(
        "--color",
        default="none",
        choices=["none", "vibrant", "rave", "warm", "cool"],
        help="Video color grade: 'none' (original), 'vibrant' / 'rave' (+25%% sat, +10%% contrast), 'warm' (warmer club tones), 'cool' (cooler cyber/blue tones).",
    )
    p.add_argument(
        "--mode",
        default="fullscreen",
        choices=["fullscreen", "fill", "blur", "landscape", "original", "compare"],
        help="Video layout mode: 'fullscreen' / 'fill' (default: edge-to-edge 9:16 vertical crop), 'blur' (blurred background bars), 'landscape' / 'original' (16:9 widescreen), or 'compare' (side-by-side comparison).",
    )
    p.add_argument(
        "--both",
        action="store_true",
        help="Export both styles ('_fullscreen.mp4' and '_blur.mp4') AND a side-by-side comparison video ('_compare.mp4') simultaneously.",
    )
    p.add_argument(
        "--compare",
        action="store_true",
        help="Export a synchronized side-by-side comparison video to preview both fullscreen and blur layouts together.",
    )
    p.add_argument("--blur", action="store_true", help="Use blurred background fill instead of full-screen vertical crop (shortcut for --mode blur)")
    p.add_argument("--landscape", action="store_true", help="Keep original 16:9 widescreen full screen (shortcut for --mode landscape)")
    p.add_argument("--no-blur", action="store_true", help="Deprecated shortcut for full-screen mode (now default)")
    p.add_argument(
        "--max-res",
        default="1080",
        choices=["720", "1080", "1440", "2160", "max"],
        help="Maximum video height to download (default: 1080). 1080p is optimal for vertical shorts and downloads significantly faster.",
    )
    p.add_argument("--cpu", action="store_true", help="Force CPU software encoding (libx264) instead of auto-detecting GPU hardware acceleration.")
    p.add_argument(
        "--preview",
        action="store_true",
        help="Faster/lower-quality encode for discovery review previews (not for posting).",
    )
    p.add_argument("--save-audio", action="store_true", help="Also save a standalone audio file (e.g. .mp3) alongside the vertical video short.")
    p.add_argument("--audio-only", action="store_true", help="Only download and export the standalone audio file, skipping video rendering.")
    p.add_argument("--audio-format", default="mp3", choices=["mp3", "wav", "m4a", "flac"], help="Format for standalone audio export (default: mp3).")
    p.add_argument("--audio-output", default=None, help="Custom filename for the exported audio file (default: matches --output with audio extension).")
    p.add_argument(
        "--post",
        default=None,
        help="Target platform(s) to post to after rendering ('all', 'youtube', 'instagram', 'tiktok', or comma-separated e.g. 'youtube,instagram').",
    )
    p.add_argument("--caption", default=None, help="Custom caption for social media posts (default: generated from title & DJ hashtags).")
    p.add_argument("--tags", default=None, help="Comma-separated hashtags or tags for social media posts (e.g. 'dj,rave,techno').")
    p.add_argument(
        "--privacy",
        default="public",
        choices=["public", "unlisted", "private"],
        help="Privacy status for YouTube Shorts upload (default: public).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the social media upload process and validate video parameters without publishing online.",
    )
    p.add_argument(
        "--choose",
        default="fullscreen",
        choices=["fullscreen", "blur"],
        help="When generating both formats (--both), choose which layout to publish to social media ('fullscreen' or 'blur', default: fullscreen). The unchosen layout will be automatically deleted.",
    )
    p.add_argument("--config", default="config.json", help="Path to config.json containing social media credentials (default: config.json).")
    p.add_argument(
        "--segment-only",
        action="store_true",
        help="Download/extract only the raw video segment without applying overlays or rendering, and save to --output.",
    )
    args = p.parse_args()

    check_deps()

    resolved_font = resolve_font(args.font)
    if not args.audio_only and not os.path.isfile(resolved_font):
        sys.exit(f"Font file not found: {args.font} (resolved to: {resolved_font})\nPass a valid font name or path with --font.")

    # Resolve layout mode
    if args.compare:
        mode = "compare"
    elif args.blur:
        mode = "blur"
    elif args.landscape:
        mode = "landscape"
    else:
        mode = args.mode
    if mode == "fill":
        mode = "fullscreen"

    # Determine default audio output path if requested
    audio_output = args.audio_output
    if (args.save_audio or args.audio_only) and not audio_output:
        base, _ = os.path.splitext(args.output)
        audio_output = f"{base}.{args.audio_format}"

    # Create temporary directory (ignore cleanup errors on Windows to avoid lock issues)
    temp_dir_kwargs = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}
    with tempfile.TemporaryDirectory(**temp_dir_kwargs) as workdir:
        raw_path = download_clip(args.url, args.start, args.end, workdir, max_res=args.max_res, audio_only=args.audio_only)

        if args.segment_only:
            out_dir = os.path.dirname(args.output)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            cmd = ["ffmpeg", "-y", "-i", raw_path, "-c", "copy", args.output]
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode != 0:
                subprocess.run(["ffmpeg", "-y", "-i", raw_path, "-c:v", "libx264", "-c:a", "aac", args.output], check=True)
            print(f"\nDone! Raw segment saved to {args.output}")
            sys.exit(0)

        if args.audio_only:
            extract_audio(raw_path, audio_output, audio_format=args.audio_format, title=args.title)
            print(f"\nDone! Saved audio to {audio_output}")
        elif args.both:
            previews_dir = os.path.join("workspace", "previews")
            os.makedirs(previews_dir, exist_ok=True)
            out_dir = os.path.dirname(args.output)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            base, ext = os.path.splitext(args.output)
            if not ext:
                ext = ".mp4"
            stem = os.path.basename(base)
            fullscreen_out = f"{base}_fullscreen{ext}"
            blur_out = f"{base}_blur{ext}"
            compare_out = os.path.join(previews_dir, f"{stem}_compare{ext}")

            print("\n========================================")
            print("1/3: Rendering Full Screen (9:16) Short...")
            print("========================================")
            build_short(
                raw_path, args.title, fullscreen_out, resolved_font, "fullscreen", workdir,
                text_style=args.text_style, color_style=args.color, font_size=args.font_size,
                title_y=args.title_y, force_cpu=args.cpu, preview=args.preview
            )

            print("\n========================================")
            print("2/3: Rendering Blurred Bars (9:16) Short...")
            print("========================================")
            build_short(
                raw_path, args.title, blur_out, resolved_font, "blur", workdir,
                text_style=args.text_style, color_style=args.color, font_size=args.font_size,
                title_y=args.title_y, force_cpu=args.cpu, preview=args.preview
            )

            print("\n========================================")
            print("3/3: Rendering Synchronized Side-by-Side Comparison Video...")
            print("========================================")
            build_comparison(
                raw_path, args.title, compare_out, resolved_font,
                text_style=args.text_style, color_style=args.color, font_size=args.font_size,
                title_y=args.title_y, workdir=workdir, force_cpu=args.cpu
            )

            if args.save_audio:
                extract_audio(raw_path, audio_output, audio_format=args.audio_format, title=args.title)
                print(f"Saved audio to {audio_output}")

            print(f"\nAll done! Successfully generated:")
            print(f"  - Full screen short:      {fullscreen_out}")
            print(f"  - Blurred bars short:     {blur_out}")
            print(f"  - Side-by-side reference: {compare_out} (saved to workspace/previews/)")
            if args.save_audio:
                print(f"  - Audio file:             {audio_output}")
        elif mode == "compare":
            previews_dir = os.path.join("workspace", "previews")
            os.makedirs(previews_dir, exist_ok=True)
            dest = os.path.join(previews_dir, os.path.basename(args.output))
            if not dest.lower().endswith(".mp4"):
                dest = f"{dest}.mp4"
            if "_compare" not in os.path.basename(dest).lower():
                stem, ext = os.path.splitext(os.path.basename(dest))
                dest = os.path.join(previews_dir, f"{stem}_compare{ext or '.mp4'}")
            print("\nRendering Side-by-Side Comparison Video...")
            build_comparison(
                raw_path, args.title, dest, resolved_font,
                text_style=args.text_style, color_style=args.color, font_size=args.font_size,
                title_y=args.title_y, workdir=workdir, force_cpu=args.cpu
            )
            if args.save_audio:
                extract_audio(raw_path, audio_output, audio_format=args.audio_format, title=args.title)
                print(f"Saved audio to {audio_output}")
            print(f"\nDone! Saved reference comparison video to {dest}")
        else:
            out_dir = os.path.dirname(args.output)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            build_short(
                raw_path, args.title, args.output, resolved_font, mode, workdir,
                text_style=args.text_style, color_style=args.color, font_size=args.font_size,
                title_y=args.title_y, force_cpu=args.cpu, preview=args.preview
            )
            if args.save_audio:
                extract_audio(raw_path, audio_output, audio_format=args.audio_format, title=args.title)
                print(f"\nDone! Saved video to {args.output} and audio to {audio_output}")
            else:
                print(f"\nDone! Saved to {args.output}")

        # Optional Social Media Publishing
        if args.post:
            if args.audio_only:
                print("\n[Warning] Cannot post audio-only file to video platforms (YouTube Shorts, Reels, TikTok).")
            else:
                try:
                    from uploader import post_to_all
                    if args.both:
                        if args.choose == "blur":
                            target_video = blur_out
                            unused_video = fullscreen_out
                        else:
                            target_video = fullscreen_out
                            unused_video = blur_out
                    else:
                        target_video = os.path.join("workspace", "previews", os.path.basename(args.output)) if mode == "compare" else args.output
                        unused_video = None

                    platforms = [p.strip().lower() for p in args.post.split(",") if p.strip()]
                    tags_list = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

                    print("\n========================================")
                    print(f"Publishing to Social Media: {', '.join(platforms)}")
                    print(f"Selected Layout: {args.choose if args.both else 'Default'}")
                    print("========================================")

                    post_to_all(
                        video_path=target_video,
                        title=args.title,
                        caption=args.caption,
                        tags=tags_list,
                        platforms=platforms,
                        config_path=args.config,
                        dry_run=args.dry_run,
                        privacy=args.privacy,
                        move_to_posted=True,
                    )

                    # Delete unused alternative layout if requested
                    if unused_video and os.path.isfile(unused_video):
                        try:
                            os.remove(unused_video)
                            print(f"[Cleanup] Deleted unused layout alternative: {unused_video}")
                        except Exception as e:
                            print(f"[Warning] Could not remove unused layout: {e}")

                except ImportError as e:
                    print(f"\n[Error] Could not import uploader dependencies: {e}")
                    print("Run: pip install -r requirements.txt")
                except Exception as e:
                    print(f"\n[Error] Social media posting failed: {e}")


if __name__ == "__main__":
    main()
