#!/usr/bin/env python3
"""
uploader.py - Multi-platform social media upload automation for YouTube Shorts, Instagram Reels, and TikTok.

Usage examples:
    # Dry run (verify video format, metadata, and config without uploading live)
    python uploader.py --video myclip.mp4 --platforms youtube,instagram,tiktok --title "FRED AGAIN" --dry-run

    # Post to all configured platforms
    python uploader.py --video myclip.mp4 --platforms all --title "FRED AGAIN"

    # Post to YouTube Shorts only (as unlisted for testing)
    python uploader.py --video myclip.mp4 --platforms youtube --title "FRED AGAIN" --privacy unlisted

    # Run interactive setup wizard to configure account credentials
    python uploader.py --setup
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Default hashtag recommendations for DJ sets & music shorts
DEFAULT_HASHTAGS = ["#Shorts", "#DJ", "#ElectronicMusic", "#EDM", "#BoilerRoom", "#Festival", "#DJSet"]


def load_config(config_path: str = "config.json") -> dict:
    """Load configuration from config.json and/or environment variables."""
def resolve_config_path(config_path: str = "clients.json") -> str:
    """Resolve preferred config path (clients.json > config.json)."""
    if os.path.isfile(config_path):
        return config_path
    if config_path in ("clients.json", "config.json"):
        for alt in ("clients.json", "config.json"):
            if os.path.isfile(alt):
                return alt
    return config_path


def save_clients_config(cfg: dict, config_path: str = "clients.json") -> bool:
    """Save configuration back to clients.json (or config.json)."""
    target = resolve_config_path(config_path)
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        print(f"Warning: Failed to save {target}: {e}")
        return False


def _persist_youtube_creds(creds, token_file: str = "token.json", config_path: str = "clients.json") -> None:
    """Persist updated OAuth credentials to clients.json and/or token.json."""
    creds_dict = json.loads(creds.to_json()) if hasattr(creds, "to_json") else creds
    cfg_file = resolve_config_path(config_path)
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "youtube" not in cfg:
                cfg["youtube"] = {}
            cfg["youtube"]["token"] = creds_dict
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not update {cfg_file} with YouTube token: {e}")

    if token_file:
    if token_file and (os.path.isfile(token_file) or not os.path.isfile(cfg_file)):
        try:
            with open(token_file, "w", encoding="utf-8") as f:
                if hasattr(creds, "to_json"):
                    f.write(creds.to_json())
                else:
                    json.dump(creds_dict, f, indent=2)
        except Exception:
            pass


def load_config(config_path: str = "clients.json") -> dict:
    """Load configuration from clients.json / config.json and/or environment variables."""
    actual_path = resolve_config_path(config_path)

    yt_secrets = os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secrets.json")
    if not os.path.isfile(yt_secrets) and os.path.isfile("client_secrets.json.json"):
        yt_secrets = "client_secrets.json.json"

    config = {
        "youtube": {
            "enabled": True,
            "client_secrets_file": yt_secrets,
            "token_file": os.environ.get("YOUTUBE_TOKEN_FILE", "token.json"),
            "default_privacy": "public",
        },
        "instagram": {
            "enabled": True,
            "username": os.environ.get("INSTAGRAM_USERNAME", ""),
            "password": os.environ.get("INSTAGRAM_PASSWORD", ""),
            "sessionid": os.environ.get("INSTAGRAM_SESSIONID", ""),
            "session_file": os.environ.get("INSTAGRAM_SESSION_FILE", "instagram_session.json"),
            # Browser profile used for Playwright uploads (instagrapi/CAA is unreliable)
            "session_dir": os.environ.get("INSTAGRAM_SESSION_DIR", "instagram_browser"),
        },
        "tiktok": {
            "enabled": True,
            "session_dir": os.environ.get("TIKTOK_SESSION_DIR", "tiktok_session"),
        },
    }

    # If config file exists, merge it
    if config_path and os.path.isfile(config_path):
    if actual_path and os.path.isfile(actual_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
            with open(actual_path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                for section, values in user_cfg.items():
                    if section in config and isinstance(values, dict):
                        config[section].update(values)
                    elif section not in config:
                        config[section] = values
        except Exception as e:
            print(f"Warning: Failed to parse config.json: {e}")
            print(f"Warning: Failed to parse {actual_path}: {e}")

    # If instagram has authorization_data in config, extract sessionid if not explicitly set
    if "instagram" in config and isinstance(config["instagram"], dict):
        auth_data = config["instagram"].get("authorization_data")
        if isinstance(auth_data, dict) and not config["instagram"].get("sessionid"):
            config["instagram"]["sessionid"] = auth_data.get("sessionid", "")

    # If python-dotenv is available, also load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
        if os.environ.get("INSTAGRAM_USERNAME"):
            config["instagram"]["username"] = os.environ["INSTAGRAM_USERNAME"]
        if os.environ.get("INSTAGRAM_PASSWORD"):
            config["instagram"]["password"] = os.environ["INSTAGRAM_PASSWORD"]
        if os.environ.get("INSTAGRAM_SESSIONID"):
            config["instagram"]["sessionid"] = os.environ["INSTAGRAM_SESSIONID"]
    except ImportError:
        pass

    return config


def get_video_info(video_path: str) -> dict:
    """Extract duration, resolution, and audio info using ffprobe or ffmpeg."""
    info = {"duration": 0.0, "width": 0, "height": 0, "is_vertical": False, "has_audio": False}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration",
            "-of", "json",
            video_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            fmt = data.get("format", {})
            if streams:
                info["width"] = streams[0].get("width", 0)
                info["height"] = streams[0].get("height", 0)
                if "duration" in streams[0]:
                    info["duration"] = float(streams[0]["duration"])
            if info["duration"] == 0.0 and "duration" in fmt:
                info["duration"] = float(fmt["duration"])
            info["is_vertical"] = info["height"] > info["width"]
    except Exception:
        pass
    return info


def validate_video_for_shorts(video_path: str) -> tuple[bool, list[str]]:
    """Verify that video meets the requirements for YouTube Shorts, Reels, and TikTok."""
    warnings = []
    if not os.path.isfile(video_path):
        return False, [f"File does not exist: {video_path}"]

    normalized = os.path.normpath(video_path).lower()
    base_name = os.path.basename(normalized)

    # Strict safeguard: NEVER allow side-by-side comparison reference videos to be posted
    if "_compare" in base_name or "previews" in normalized.split(os.sep):
        return False, [
            f"Blocked: '{video_path}' is a reference comparison video.\n"
            "Comparison videos are for quick local reference only and cannot be posted to social media.\n"
            "Please select the Full Screen or Blurred version to post."
        ]

    info = get_video_info(video_path)
    if info["duration"] > 60.5:
        warnings.append(f"Duration is {info['duration']:.1f}s (YouTube Shorts requires <=60s).")
    if not info["is_vertical"]:
        warnings.append(f"Resolution is {info['width']}x{info['height']} (Vertical 9:16 format is strongly recommended).")

    return True, warnings


def build_caption(title: str, custom_caption: str = "", tags: list[str] | None = None) -> tuple[str, str, list[str]]:
    """Build platform-optimized title, description/caption, and tags.

    Always appends default DJ hashtags at the end of the caption so the user
    only needs to write the sentence — tags are added automatically.
    """
    tag_list = list(tags) if tags else list(DEFAULT_HASHTAGS)

    # Ensure #Shorts is present for YouTube
    if "#Shorts" not in tag_list and "#shorts" not in tag_list:
        tag_list.append("#Shorts")

    clean_tags = [t if t.startswith("#") else f"#{t}" for t in tag_list]
    raw_tag_keywords = [t.lstrip("#") for t in clean_tags]

    body = (custom_caption or title or "").rstrip()
    body_lower = body.lower()

    # Only append tags that are not already in the caption
    missing_tags = [t for t in clean_tags if t.lower() not in body_lower]
    tag_str = " ".join(missing_tags)

    if body and tag_str:
        full_caption = f"{body}\n\n{tag_str}"
    elif body:
        full_caption = body
    else:
        full_caption = " ".join(clean_tags)

    yt_title = f"{title} #Shorts" if title and "#shorts" not in title.lower() else (title or "DJ Set Clip #Shorts")

    return yt_title, full_caption, raw_tag_keywords


# ==============================================================================
# 1. YouTube Shorts Publisher (Official Google YouTube Data API v3)
# ==============================================================================

def upload_youtube_short(
    video_path: str,
    title: str,
    description: str,
    tags: list[str] | None = None,
    privacy_status: str = "public",
    client_secrets_file: str = "client_secrets.json",
    token_file: str = "token.json",
    token_data: dict | None = None,
    client_secrets_data: dict | None = None,
) -> dict:
    """Upload vertical short to YouTube using official Google API with OAuth2."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        return {
            "success": False,
            "error": "Missing Google API libraries. Run: pip install google-api-python-client google-auth-oauthlib",
        }

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = None

    if os.path.exists(token_file):
    if token_data and isinstance(token_data, dict):
        try:
            creds = Credentials.from_authorized_user_info(token_data, scopes)
        except Exception:
            creds = None

    if not creds and os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, scopes)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                _persist_youtube_creds(creds, token_file=token_file)
            except Exception:
                creds = None

        if not creds:
            if not os.path.exists(client_secrets_file):
                if os.path.exists(f"{client_secrets_file}.json"):
                    client_secrets_file = f"{client_secrets_file}.json"
                elif os.path.exists("client_secrets.json.json"):
                    client_secrets_file = "client_secrets.json.json"
                else:
                    return {
                        "success": False,
                        "error": (
                            f"YouTube client secrets file not found: {client_secrets_file}.\n"
                            "To set up YouTube upload:\n"
                            "  1. Go to Google Cloud Console (https://console.cloud.google.com/)\n"
                            "  2. Create a project and enable 'YouTube Data API v3'\n"
                            "  3. Create OAuth 2.0 Client ID credentials (Desktop app)\n"
                            "  4. Download JSON and save as 'client_secrets.json' in this folder."
                        ),
                    }
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes)
            if client_secrets_data and isinstance(client_secrets_data, dict):
                flow = InstalledAppFlow.from_client_config(client_secrets_data, scopes)
            else:
                if not os.path.exists(client_secrets_file):
                    if os.path.exists(f"{client_secrets_file}.json"):
                        client_secrets_file = f"{client_secrets_file}.json"
                    elif os.path.exists("client_secrets.json.json"):
                        client_secrets_file = "client_secrets.json.json"
                    else:
                        return {
                            "success": False,
                            "error": (
                                f"YouTube client secrets file not found: {client_secrets_file}.\n"
                                "To set up YouTube upload:\n"
                                "  1. Go to Google Cloud Console (https://console.cloud.google.com/)\n"
                                "  2. Create a project and enable 'YouTube Data API v3'\n"
                                "  3. Create OAuth 2.0 Client ID credentials (Desktop app)\n"
                                "  4. Download JSON and save into clients.json (or as 'client_secrets.json' in this folder)."
                            ),
                        }
                flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes)
            creds = flow.run_local_server(port=0)
            _persist_youtube_creds(creds, token_file=token_file)

        with open(token_file, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags or [],
            "categoryId": "10",  # 10 = Music
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    print(f"Uploading to YouTube Shorts ({privacy_status})...")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  YouTube upload progress: {int(status.progress() * 100)}%")

    video_id = response.get("id")
    shorts_url = f"https://youtube.com/shorts/{video_id}"
    return {"success": True, "platform": "YouTube", "video_id": video_id, "url": shorts_url}


# ==============================================================================
# 2. Instagram Reels Publisher (Browser automation via Playwright)
# ==============================================================================
# Instagram blocks password / instagrapi mobile-API logins with CAA.
# We upload through the same logged-in Chromium profile used during setup.

def _instagram_setup_help(session_dir: str = "instagram_browser") -> str:
    return (
        "Instagram requires a one-time browser login (password/API login is blocked by CAA).\n"
        "Run:\n"
        "  python uploader.py --setup-instagram\n"
        "Or click 'Instagram Login' in the Web Studio, then retry the post.\n"
        f"(Session folder: {session_dir}/)"
    )


def _ig_click_first(page, selectors: list[str], timeout_ms: int = 4000) -> bool:
    """Click the first visible matching locator."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=timeout_ms)
                return True
        except Exception:
            continue
    return False


def _ig_click_role_button(page, name: str, timeout_ms: int = 20000, optional: bool = False) -> bool:
    """Click an Instagram modal button by accessible name (Next / Share / OK)."""
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        candidates = [
            page.get_by_role("button", name=name, exact=True),
            page.locator(f'div[role="button"]:text-is("{name}")'),
            page.locator(f'div[role="button"]:has-text("{name}")'),
            page.locator(f'button:text-is("{name}")'),
            page.locator(f'[role="button"]:text-is("{name}")'),
        ]
        for loc in candidates:
            try:
                btn = loc.first
                if btn.count() == 0:
                    continue
                if not btn.is_visible():
                    continue
                # Wait for enabled when possible (video processing disables Next)
                try:
                    if hasattr(btn, "is_enabled") and not btn.is_enabled():
                        page.wait_for_timeout(500)
                        continue
                except Exception:
                    pass
                btn.click(timeout=3000)
                return True
            except Exception:
                continue
        page.wait_for_timeout(500)

    if optional:
        return False
    return False


def _ig_fill_caption(page, caption: str) -> None:
    selectors = [
        'textarea[aria-label^="Write a caption"]',
        'div[aria-label^="Write a caption"][contenteditable="true"]',
        "div[aria-label='Write a caption...']",
        "textarea[aria-label='Write a caption...']",
        "div[aria-label='Write a caption…']",
        "div[role='textbox'][contenteditable='true']",
    ]
    text = (caption or "")[:2200]
    for sel in selectors:
        try:
            box = page.locator(sel).first
            if box.count() == 0 or not box.is_visible():
                continue
            box.click(timeout=3000)
            try:
                box.fill("")
                box.type(text, delay=20)
            except Exception:
                box.fill(text)
            return
        except Exception:
            try:
                box.click(timeout=3000)
                page.keyboard.type(text, delay=15)
                return
            except Exception:
                continue


def _ig_dump_debug(page, label: str = "ig_debug") -> str | None:
    """Save a screenshot + button inventory when the create flow stalls."""
    try:
        os.makedirs("scratch", exist_ok=True)
        path = os.path.join("scratch", f"{label}_{int(time.time())}.png")
        page.screenshot(path=path, full_page=True)
        try:
            texts = page.evaluate(
                """() => Array.from(document.querySelectorAll('button, [role="button"]'))
                    .slice(0, 40)
                    .map(el => (el.innerText || el.getAttribute('aria-label') || '').trim())
                    .filter(Boolean)"""
            )
            print(f"  [debug] Visible buttons: {texts}")
        except Exception:
            pass
        print(f"  [debug] Screenshot saved: {path}")
        return path
    except Exception as e:
        print(f"  [debug] Could not save screenshot: {e}")
        return None


def _ig_open_new_post(page) -> bool:
    """Click the sidebar New post (+) control."""
    selectors = [
        'svg[aria-label="New post"]',
        '[aria-label="New post"]',
        'svg[aria-label="New post"] title',
        'div[role="link"]:has(svg[aria-label="New post"])',
        'a:has(svg[aria-label="New post"])',
        'span:text-is("Create")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            # title node is not clickable — click parent svg
            if sel.endswith(" title"):
                loc = page.locator('svg[aria-label="New post"]').first
            loc.click(timeout=5000, force=True)
            return True
        except Exception:
            continue
    # JS fallback: find by aria-label and click closest clickable ancestor
    try:
        clicked = page.evaluate(
            """() => {
                const el = document.querySelector('[aria-label="New post"], svg[aria-label="New post"]');
                if (!el) return false;
                const target = el.closest('[role="link"], [role="button"], a, div') || el;
                target.click();
                return true;
            }"""
        )
        return bool(clicked)
    except Exception:
        return False


def _ig_attach_video(page, abs_video: str) -> bool:
    """
    After New post is open: click 'Select from computer' and attach the video.
    Prefers Playwright's file chooser API; falls back to hidden <input type=file>.
    """
    select_btn_selectors = [
        'button:has-text("Select from computer")',
        'button:has-text("Select from Computer")',
        'button._aswp:has-text("Select")',
        '[role="button"]:has-text("Select from computer")',
    ]

    # Wait for the create modal / select button to appear
    select_btn = None
    for _ in range(20):
        for sel in select_btn_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    select_btn = loc
                    break
            except Exception:
                continue
        if select_btn:
            break
        page.wait_for_timeout(500)

    # Path A: click Select from computer and catch the native file chooser
    if select_btn:
        try:
            with page.expect_file_chooser(timeout=15000) as fc_info:
                select_btn.click(timeout=5000)
            chooser = fc_info.value
            chooser.set_files(abs_video)
            print("  Attached video via file chooser.")
            return True
        except Exception as e:
            print(f"  File chooser path failed ({e}); trying hidden file input...")

    # Path B: set files on a hidden input (may already exist, or appear after click)
    _ig_click_first(page, select_btn_selectors)
    page.wait_for_timeout(800)

    file_selectors = [
        'input[type="file"][accept*="video"]',
        'input[type="file"][accept*="image"]',
        'form[role="presentation"] input[type="file"]',
        'input[type="file"]',
    ]
    for _ in range(15):
        for sel in file_selectors:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            try:
                # Use the last input — IG sometimes keeps stale ones in the DOM
                loc.nth(loc.count() - 1).set_input_files(abs_video)
                print("  Attached video via hidden file input.")
                return True
            except Exception:
                try:
                    loc.first.set_input_files(abs_video)
                    print("  Attached video via hidden file input.")
                    return True
                except Exception:
                    continue
        page.wait_for_timeout(400)

    return False


def upload_instagram_reel(
    video_path: str,
    caption: str,
    username: str = "",
    password: str = "",
    sessionid: str = "",
    session_file: str = "instagram_session.json",
    session_dir: str = "instagram_browser",
    headless: bool = True,
) -> dict:
    """Upload a vertical short as an Instagram Reel via Instagram.com (Playwright)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False,
            "error": "Missing playwright library. Run: pip install playwright && playwright install chromium",
        }

    abs_video = os.path.abspath(video_path)
    abs_session = os.path.abspath(session_dir)

    if not os.path.isdir(abs_session) or not os.listdir(abs_session):
        return {
            "success": False,
            "error": (
                f"Instagram browser session '{session_dir}' not found.\n"
                f"{_instagram_setup_help(session_dir)}"
            ),
        }

    print("Launching Instagram uploader session...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=abs_session,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = browser.new_page()

            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            if "accounts/login" in page.url.lower() or page.locator("input[name='username']").count() > 0:
                browser.close()
                return {
                    "success": False,
                    "error": (
                        "Instagram session expired or not logged in.\n"
                        f"{_instagram_setup_help(session_dir)}"
                    ),
                }

            _ig_click_first(page, [
                "button:has-text('Not Now')",
                "button:has-text('Not now')",
                "button:has-text('Allow all cookies')",
                "button:has-text('Accept')",
            ])

            # 1) Click New post (+) in the left sidebar
            print("Clicking Instagram New post...")
            if not _ig_open_new_post(page):
                _ig_dump_debug(page, "ig_no_new_post")
                browser.close()
                return {"success": False, "error": "Could not click Instagram 'New post' (+) button."}
            page.wait_for_timeout(1500)

            # Optional: pick Post/Reel from create menu if shown
            _ig_click_first(page, [
                'span:text-is("Post")',
                'div[role="menuitem"]:has-text("Post")',
                'span:text-is("Reel")',
                'div[role="menuitem"]:has-text("Reel")',
            ])
            page.wait_for_timeout(1000)

            # 2) Click "Select from computer" and 3) attach the video
            print("Clicking 'Select from computer' and attaching video...")
            if not _ig_attach_video(page, abs_video):
                _ig_dump_debug(page, "ig_no_file_input")
                browser.close()
                return {
                    "success": False,
                    "error": "Could not attach video after 'Select from computer'.",
                }

            print("Waiting for Instagram to process video...")
            page.wait_for_timeout(4000)

            # Reel confirmation dialog ("Video posts are now shared as reels")
            _ig_click_role_button(page, "OK", timeout_ms=5000, optional=True)
            page.wait_for_timeout(1000)

            # Crop step → Next (wait longer; Next stays disabled while processing)
            print("Stepping through Instagram create flow...")
            if not _ig_click_role_button(page, "Next", timeout_ms=90000):
                _ig_dump_debug(page, "ig_no_next_crop")
                browser.close()
                return {"success": False, "error": "Could not find Instagram Next button after upload (video may still be processing)."}
            page.wait_for_timeout(2000)

            # Filters / cover step → Next (optional for some Reel paths)
            _ig_click_role_button(page, "Next", timeout_ms=15000, optional=True)
            page.wait_for_timeout(2000)

            _ig_fill_caption(page, caption)
            page.wait_for_timeout(1000)

            print("Clicking Instagram Share...")
            shared = _ig_click_role_button(page, "Share", timeout_ms=30000)
            if not shared:
                if _ig_click_role_button(page, "Next", timeout_ms=8000, optional=True):
                    page.wait_for_timeout(1500)
                    _ig_fill_caption(page, caption)
                    shared = _ig_click_role_button(page, "Share", timeout_ms=20000)

            if not shared:
                _ig_dump_debug(page, "ig_no_share")
                browser.close()
                return {
                    "success": False,
                    "error": (
                        "Could not find Instagram Share button.\n"
                        "A debug screenshot was saved under scratch/."
                    ),
                }

            print("Waiting for Instagram to finish posting...")
            success, post_url = _ig_wait_until_posted(
                page,
                username=username,
                timeout_s=180,
            )

            profile = username.strip().lstrip("@") if username else ""
            url = post_url or (f"https://www.instagram.com/{profile}/" if profile else "https://www.instagram.com/")

            if not success:
                # Give IG one more settle window before giving up
                print("  Still waiting a bit longer for post confirmation...")
                page.wait_for_timeout(15000)
                success, post_url = _ig_wait_until_posted(
                    page,
                    username=username,
                    timeout_s=60,
                )
                url = post_url or url

            if not success:
                _ig_dump_debug(page, "ig_share_unconfirmed")
                browser.close()
                return {
                    "success": False,
                    "error": (
                        "Share was clicked but Instagram did not confirm the post finished. "
                        "Check the account manually — it may still have published. "
                        "Debug screenshot saved under scratch/."
                    ),
                    "url": url,
                }

            # Brief linger so IG can finish any final upload handshake before the profile closes
            page.wait_for_timeout(5000)
            browser.close()
            return {"success": True, "platform": "Instagram", "url": url}
    except Exception as e:
        return {"success": False, "error": f"Instagram upload failed: {e}"}


def _ig_page_indicates_shared(page) -> bool:
    """True when IG shows the 'Your reel has been shared' success screen."""
    # Exact success UI the Instagram create modal shows
    exact_selectors = [
        'h3:has-text("Your reel has been shared")',
        'h3:has-text("Your post has been shared")',
        'h3:has-text("Your reel has been shared.")',
        'h3:has-text("Your post has been shared.")',
        'div[role="heading"]:has-text("Shared reel")',
        'div[role="heading"]:has-text("Shared post")',
        'img[alt="Animated checkmark"]',
        'div[role="button"]:text-is("Done")',
    ]
    for sel in exact_selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue

    success_phrases = (
        "your reel has been shared",
        "your post has been shared",
        "your video has been shared",
        "shared reel",
        "shared post",
    )
    try:
        body = (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception:
        body = ""
    if any(p in body for p in success_phrases):
        return True

    for sel in (
        'img[alt*="checkmark" i]',
        'img[alt*="shared" i]',
        'svg[aria-label*="shared" i]',
        '[aria-label*="shared" i]',
    ):
        try:
            if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                return True
        except Exception:
            pass
    return False


def _ig_click_done_after_share(page) -> None:
    """Click the Done button on the Shared reel success dialog."""
    _ig_click_first(page, [
        'div[role="button"]:text-is("Done")',
        'button:text-is("Done")',
        'div[role="button"]:has-text("Done")',
        "button:has-text('Done')",
        "button:has-text('OK')",
        "div[role='button']:has-text('Close')",
        "button:has-text('Close')",
    ])
    page.wait_for_timeout(1000)


def _ig_share_still_in_progress(page) -> bool:
    """True while IG is still uploading / sharing after Share click."""
    try:
        body = (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception:
        body = ""
    if "sharing..." in body or "uploading" in body or "processing" in body:
        return True
    if "please wait" in body or "creating your" in body:
        return True
    # Share button still visible usually means it hasn't finished transitioning
    try:
        share = page.get_by_role("button", name="Share", exact=True).first
        if share.count() > 0 and share.is_visible():
            # Disabled Share often means upload in progress after click
            try:
                if not share.is_enabled():
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _ig_extract_posted_url(page) -> str | None:
    """Try to pull a /reel/ or /p/ link from the success screen."""
    try:
        for sel in ('a[href*="/reel/"]', 'a[href*="/p/"]'):
            loc = page.locator(sel)
            count = min(loc.count(), 8)
            for i in range(count):
                href = loc.nth(i).get_attribute("href") or ""
                if "/reel/" in href or "/p/" in href:
                    if href.startswith("/"):
                        return "https://www.instagram.com" + href.split("?")[0]
                    if "instagram.com" in href:
                        return href.split("?")[0]
    except Exception:
        pass
    return None


def _ig_wait_until_posted(page, username: str = "", timeout_s: int = 180) -> tuple[bool, str | None]:
    """
    Block until Instagram shows 'Your reel has been shared.' (or timeout).
    Returns (success, optional_post_url).
    """
    deadline = time.time() + timeout_s
    started = time.time()
    post_url = None
    saw_busy = False
    stable_success_ticks = 0

    # Fast path: wait directly for the success heading Instagram shows
    try:
        page.locator('h3:has-text("Your reel has been shared"), h3:has-text("Your post has been shared"), div[role="heading"]:has-text("Shared reel")').first.wait_for(
            state="visible",
            timeout=min(120_000, timeout_s * 1000),
        )
        print("  [OK] Detected: Your reel has been shared.")
        post_url = _ig_extract_posted_url(page)
        _ig_click_done_after_share(page)
        page.wait_for_timeout(2000)
        return True, post_url
    except Exception:
        pass

    while time.time() < deadline:
        if _ig_share_still_in_progress(page):
            saw_busy = True
            stable_success_ticks = 0
            print("  ...still uploading/sharing")
            page.wait_for_timeout(2000)
            continue

        if _ig_page_indicates_shared(page):
            post_url = _ig_extract_posted_url(page) or post_url
            stable_success_ticks += 1
            if stable_success_ticks >= 2:
                print("  [OK] Detected: Your reel has been shared.")
                _ig_click_done_after_share(page)
                page.wait_for_timeout(1500)
                return True, post_url
            page.wait_for_timeout(800)
            continue

        # Fallback only after a long wait — prefer the explicit success screen above
        try:
            create_gone = page.locator('button:has-text("Select from computer")').count() == 0
            share_gone = True
            try:
                share_btn = page.get_by_role("button", name="Share", exact=True).first
                share_gone = share_btn.count() == 0 or not share_btn.is_visible()
            except Exception:
                pass
            on_home = "instagram.com" in page.url.lower() and "create" not in page.url.lower()
            settled_enough = saw_busy and (time.time() - started) >= 45
            if create_gone and share_gone and on_home and settled_enough:
                page.wait_for_timeout(10000)
                if _ig_page_indicates_shared(page):
                    print("  [OK] Detected: Your reel has been shared.")
                    _ig_click_done_after_share(page)
                    return True, post_url
                if not _ig_share_still_in_progress(page):
                    print("  [OK] Create dialog closed after share — treating post as finished.")
                    return True, post_url
        except Exception:
            pass

        page.wait_for_timeout(2000)

    return False, post_url


def setup_instagram_interactive(
    session_file: str = "instagram_session.json",
    session_dir: str = "instagram_browser",
) -> bool:
    """Open a visible persistent browser so the user can log into Instagram once."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Please install playwright first: pip install playwright && playwright install chromium")

    abs_session = os.path.abspath(session_dir)
    os.makedirs(abs_session, exist_ok=True)

    print("\n========================================================")
    print("Instagram Interactive Login Setup")
    print("========================================================")
    print(f"Opening browser using session folder: {abs_session}")
    print("Please log into your Instagram account in the browser window.")
    print("Once you see your home feed / profile, this script will save the session automatically.\n")

    logged_in = False
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=abs_session,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = browser.new_page()
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=60000)

        print("Waiting for Instagram login in browser window (will auto-detect once logged in)...")
        for _ in range(240):
            page.wait_for_timeout(1000)
            url = page.url.lower()
            cookies = browser.cookies()
            has_sid = any(
                c.get("name") == "sessionid" and len(c.get("value", "")) > 10
                for c in cookies
            )
            on_login = "accounts/login" in url or "challenge" in url
            # Home / profile signals
            try:
                has_home = page.locator("svg[aria-label='Home'], svg[aria-label='New post'], a[href='/']").count() > 0
            except Exception:
                has_home = False

            if has_sid and (has_home or not on_login):
                # Extra settle so cookies are fully written to the profile
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
                logged_in = True
                print("[OK] Instagram login detected successfully!")
                break

        browser.close()

    if not logged_in:
        print("\n[Error] Could not confirm Instagram login. Complete login in the browser and try again.")
        return False

    # Optional legacy dump for tooling that still looks for the JSON file
    try:
        sid = None
        from playwright.sync_api import sync_playwright as _sp
        with _sp() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=abs_session,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            for c in browser.cookies():
                if c.get("name") == "sessionid" and len(c.get("value", "")) > 10:
                    sid = c.get("value")
                    break
            browser.close()
        if sid and session_file:
            with open(session_file, "w", encoding="utf-8") as f:
                json.dump({"authorization_data": {"sessionid": sid}, "source": "browser_setup"}, f)
        if sid:
            cfg_file = resolve_config_path("clients.json")
            if os.path.isfile(cfg_file):
                try:
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    if "instagram" not in cfg:
                        cfg["instagram"] = {}
                    cfg["instagram"]["authorization_data"] = {"sessionid": sid}
                    cfg["instagram"]["sessionid"] = sid
                    with open(cfg_file, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                except Exception:
                    pass
            if session_file and os.path.isfile(session_file):
                try:
                    with open(session_file, "w", encoding="utf-8") as f:
                        json.dump({"authorization_data": {"sessionid": sid}, "source": "browser_setup"}, f)
                except Exception:
                    pass
    except Exception:
        pass

    print(
        f"\n[Success] Instagram browser session saved to '{session_dir}/'.\n"
        "Future uploads will post through this browser profile (no CAA password login).\n"
    )
    return True


# ==============================================================================
# 3. TikTok Publisher (Browser Automation via Playwright)
# ==============================================================================

def _dismiss_tiktok_modals(page) -> None:
    """Close TUXModal overlays that intercept clicks on the Post button."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass

    dismiss_labels = (
        "Close", "Got it", "OK", "Continue", "Not now", "Skip",
        "I understand", "Allow", "Agree", "Confirm", "Done",
    )
    for label in dismiss_labels:
        try:
            btn = page.locator(
                f".TUXModal button:has-text('{label}'), "
                f"[role='dialog'] button:has-text('{label}'), "
                f".TUXModal-overlay button:has-text('{label}')"
            ).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            pass

    for sel in (
        ".TUXModal [aria-label='Close']",
        ".TUXModal button[aria-label='Close']",
        "[role='dialog'] [aria-label='Close']",
        ".TUXModal svg[data-icon='Close']",
    ):
        try:
            closer = page.locator(sel).first
            if closer.count() > 0 and closer.is_visible():
                closer.click(timeout=2000)
                page.wait_for_timeout(400)
        except Exception:
            pass


def _tiktok_post_button_candidates(scope):
    """Yield likely Post buttons, excluding sidebar nav items."""
    selectors = [
        "button[data-e2e='post_video_button']",
        "button[data-e2e='publish-button']",
        "[data-e2e='post_video_button']",
        "div[class*='footer'] button:has-text('Post')",
        "div[class*='Footer'] button:has-text('Post')",
        "div[class*='submit'] button:has-text('Post')",
        "button:has-text('Post'):not([data-tt*='Sidebar']):not([data-tt*='sidebar'])",
        "button[class*='Button']:has-text('Post'):not([data-tt*='Sidebar'])",
    ]
    for sel in selectors:
        try:
            loc = scope.locator(sel)
            count = loc.count()
            for i in range(count):
                yield loc.nth(i)
        except Exception:
            continue


def _find_tiktok_post_button(page):
    """Find the real upload Post button (not the sidebar 'Post' link)."""
    scopes = [page] + list(page.frames)
    for scope in scopes:
        for btn in _tiktok_post_button_candidates(scope):
            try:
                if not btn.is_visible() or not btn.is_enabled():
                    continue
                # Skip sidebar / nav lookalikes
                data_tt = (btn.get_attribute("data-tt") or "").lower()
                if "sidebar" in data_tt or "nav" in data_tt:
                    continue
                text = (btn.inner_text() or "").strip().lower()
                if text and text != "post" and "post" not in text:
                    continue
                return btn
            except Exception:
                continue
    return None


def upload_tiktok_video(
    video_path: str,
    caption: str,
    session_dir: str = "tiktok_session",
    headless: bool = True,
) -> dict:
    """Upload video to TikTok Creator Center / TikTok Studio using Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False,
            "error": "Missing playwright library. Run: pip install playwright && playwright install chromium",
        }

    abs_video = os.path.abspath(video_path)
    abs_session = os.path.abspath(session_dir)

    if not os.path.isdir(abs_session):
        return {
            "success": False,
            "error": (
                f"TikTok session directory '{session_dir}' not found.\n"
                "Please run interactive setup once to log in: python uploader.py --setup-tiktok"
            ),
        }

    print("Launching TikTok uploader session...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=abs_session,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )
            page = browser.new_page()

            # TikTok Studio upload endpoint (with fallback to classic upload)
            upload_url = "https://www.tiktok.com/tiktokstudio/upload"
            try:
                page.goto(upload_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                page.goto("https://www.tiktok.com/upload?lang=en", wait_until="domcontentloaded", timeout=45000)

            page.wait_for_timeout(3000)
            _dismiss_tiktok_modals(page)

            # Check if redirected to login
            if "login" in page.url.lower():
                browser.close()
                return {
                    "success": False,
                    "error": "TikTok session expired or not logged in. Please run: python uploader.py --setup-tiktok",
                }

            # Locate file input selector (page or iframe)
            print("Locating video upload element on TikTok...")
            file_input = None
            for _ in range(15):
                if "login" in page.url.lower():
                    browser.close()
                    return {"success": False, "error": "TikTok session expired or not logged in. Run: python uploader.py --setup-tiktok"}

                _dismiss_tiktok_modals(page)

                inputs = page.locator("input[type='file']")
                if inputs.count() > 0:
                    file_input = inputs.first
                    break

                for frame in page.frames:
                    f_inputs = frame.locator("input[type='file']")
                    if f_inputs.count() > 0:
                        file_input = f_inputs.first
                        break

                if file_input:
                    break
                page.wait_for_timeout(1000)

            if not file_input:
                browser.close()
                return {
                    "success": False,
                    "error": "Could not locate TikTok file upload element. Check if TikTok Studio requires re-login: python uploader.py --setup-tiktok",
                }

            print("Selecting video file on TikTok...")
            file_input.set_input_files(abs_video)

            # Wait for upload processing; dismiss popups that appear mid-process
            print("Waiting for TikTok to process video...")
            post_btn = None
            for _ in range(40):
                page.wait_for_timeout(2000)
                _dismiss_tiktok_modals(page)
                post_btn = _find_tiktok_post_button(page)
                if post_btn:
                    break

            # Fill caption if available
            cb = page.locator("div[contenteditable='true']").first
            if cb.count() > 0 and cb.is_visible():
                try:
                    _dismiss_tiktok_modals(page)
                    cb.click(timeout=3000)
                    cb.fill(caption[:150])
                except Exception:
                    pass

            if post_btn and post_btn.is_enabled():
                print("Clicking TikTok Post button...")
                _dismiss_tiktok_modals(page)
                try:
                    post_btn.click(timeout=10000)
                except Exception:
                    # Overlay may still be present — force JS click on the real button
                    _dismiss_tiktok_modals(page)
                    post_btn = _find_tiktok_post_button(page) or post_btn
                    post_btn.evaluate("el => el.click()")

                # Confirm dialog after Post (if any)
                page.wait_for_timeout(1500)
                _dismiss_tiktok_modals(page)
                for confirm_label in ("Post now", "Post", "Confirm", "Publish"):
                    try:
                        confirm = page.locator(
                            f".TUXModal button:has-text('{confirm_label}'), "
                            f"[role='dialog'] button:has-text('{confirm_label}')"
                        ).first
                        if confirm.count() > 0 and confirm.is_visible():
                            confirm.click(timeout=5000)
                            break
                    except Exception:
                        pass

                page.wait_for_timeout(6000)
                browser.close()
                return {
                    "success": True,
                    "platform": "TikTok",
                    "url": "https://www.tiktok.com/tiktokstudio",
                }
            else:
                browser.close()
                return {"success": False, "error": "TikTok post button was not ready or disabled."}
    except Exception as e:
        return {"success": False, "error": f"TikTok upload failed: {e}"}


def setup_tiktok_interactive(session_dir: str = "tiktok_session"):
    """Open visible browser so user can manually log into TikTok once and save session."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Please install playwright first: pip install playwright && playwright install chromium")

    abs_session = os.path.abspath(session_dir)
    os.makedirs(abs_session, exist_ok=True)

    print("\n========================================================")
    print("TikTok Interactive Login Setup")
    print("========================================================")
    print(f"Opening browser using session folder: {abs_session}")
    print("Please log into your TikTok account in the browser window.")
    print("Once you are fully logged into TikTok and see TikTok Studio, return to this terminal and press Enter.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=abs_session,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page()
        page.goto("https://www.tiktok.com/tiktokstudio/upload")

        print("Waiting for TikTok login in browser window (will auto-detect once logged in)...")
        for _ in range(180):
            page.wait_for_timeout(1000)
            cookies = browser.cookies()
            names = [c.get("name") for c in cookies]
            if "sessionid" in names or "sessionid_ss" in names:
                print("[OK] TikTok login detected successfully!")
                break
            if "login" not in page.url.lower() and "tiktokstudio" in page.url.lower():
                if page.locator("input[type='file']").count() > 0 or page.locator("button:has-text('Post')").count() > 0:
                    print("[OK] TikTok Studio upload interface detected!")
                    break
        browser.close()

    print(f"\nSuccess! TikTok session saved to '{session_dir}'. Subsequent uploads can run headlessly.")


# ==============================================================================
# Unified Multi-Platform Dispatcher
# ==============================================================================

def post_to_all(
    video_path: str,
    title: str = "",
    caption: str = "",
    tags: list[str] | None = None,
    platforms: list[str] | str = "all",
    config_path: str = "config.json",
    config_path: str = "clients.json",
    dry_run: bool = False,
    privacy: str = "public",
    move_to_posted: bool = True,
    headless: bool = True,
) -> dict[str, dict]:
    """Publish a video short to YouTube Shorts, Instagram Reels, and/or TikTok."""
    if isinstance(platforms, str):
        selected = [p.strip().lower() for p in platforms.split(",") if p.strip()]
    else:
        selected = [p.lower() for p in platforms]

    if "all" in selected:
        selected = ["youtube", "instagram", "tiktok"]

    cfg = load_config(config_path)
    yt_title, full_caption, raw_tags = build_caption(title, caption, tags)
    valid, warnings = validate_video_for_shorts(video_path)

    if not valid:
        print("\n" + "=" * 60)
        print(">> Auto-Post Blocked by Safety Validator")
        print("=" * 60)
        for w in warnings:
            print(f"  [BLOCKED] {w}")
        return {p: {"success": False, "error": warnings[0]} for p in selected}

    results = {}

    print("\n" + "=" * 60)
    print(">> Auto-Post Automation Dispatcher")
    print("=" * 60)
    print(f"Video File:  {video_path}")
    print(f"Platforms:   {', '.join(selected).upper()}")
    print(f"Title:       {yt_title}")
    print(f"Caption:     {full_caption[:90]}...")
    if warnings:
        print("\nNotice:")
        for w in warnings:
            print(f"  [!] {w}")

    if dry_run:
        print("\n[DRY RUN MODE ENABLED] - Skipping live network uploads.")
        for p in selected:
            results[p] = {
                "success": True,
                "platform": p.capitalize(),
                "dry_run": True,
                "url": f"https://{p}.example/preview",
            }
            print(f"  [OK] [{p.upper()}] Formatted and ready for publish.")

        if move_to_posted:
            os.makedirs("posted", exist_ok=True)
            dest = os.path.join("posted", os.path.basename(video_path))
            if os.path.abspath(video_path) != os.path.abspath(dest):
                try:
                    import shutil
                    shutil.move(video_path, dest)
                    print(f"\n[OK] Moved published video to: {dest}")
                    for r in results.values():
                        r["posted_file"] = dest
                except Exception as e:
                    print(f"\n[Warning] Could not move video to posted/: {e}")

        return results

    print(f"\n>> Posting to {len(selected)} platform(s) in parallel...")
    print_lock = Lock()
    results: dict[str, dict] = {}

    def _run_youtube() -> tuple[str, dict]:
        yt_cfg = cfg.get("youtube", {})
        with print_lock:
            print("\n--- Posting to YouTube Shorts ---")
        res = upload_youtube_short(
            video_path=video_path,
            title=yt_title,
            description=full_caption,
            tags=raw_tags,
            privacy_status=privacy or yt_cfg.get("default_privacy", "public"),
            client_secrets_file=yt_cfg.get("client_secrets_file", "client_secrets.json"),
            token_file=yt_cfg.get("token_file", "token.json"),
            token_data=yt_cfg.get("token"),
            client_secrets_data=yt_cfg.get("client_secrets"),
        )
        with print_lock:
            if res.get("success"):
                print(f"  [OK] YouTube Short published: {res.get('url')}")
            else:
                print(f"  [FAIL] YouTube upload failed: {res.get('error')}")
        return "youtube", res

    def _run_instagram() -> tuple[str, dict]:
        ig_cfg = cfg.get("instagram", {})
        with print_lock:
            print("\n--- Posting to Instagram Reels ---")
        res = upload_instagram_reel(
            video_path=video_path,
            caption=full_caption,
            username=ig_cfg.get("username", ""),
            password=ig_cfg.get("password", ""),
            sessionid=ig_cfg.get("sessionid", ""),
            session_file=ig_cfg.get("session_file", "instagram_session.json"),
            session_dir=ig_cfg.get("session_dir", "instagram_browser"),
            headless=headless,
        )
        with print_lock:
            if res.get("success"):
                print(f"  [OK] Instagram Reel published: {res.get('url')}")
            else:
                print(f"  [FAIL] Instagram upload failed: {res.get('error')}")
        return "instagram", res

    def _run_tiktok() -> tuple[str, dict]:
        tt_cfg = cfg.get("tiktok", {})
        with print_lock:
            print("\n--- Posting to TikTok ---")
        res = upload_tiktok_video(
            video_path=video_path,
            caption=full_caption,
            session_dir=tt_cfg.get("session_dir", "tiktok_session"),
            headless=headless,
        )
        with print_lock:
            if res.get("success"):
                print(f"  [OK] TikTok published: {res.get('url')}")
            else:
                print(f"  [FAIL] TikTok upload failed: {res.get('error')}")
        return "tiktok", res

    jobs = []
    if "youtube" in selected:
        jobs.append(_run_youtube)
    if "instagram" in selected:
        jobs.append(_run_instagram)
    if "tiktok" in selected:
        jobs.append(_run_tiktok)

    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = [pool.submit(job) for job in jobs]
        for fut in as_completed(futures):
            try:
                plat, res = fut.result()
                results[plat] = res
            except Exception as e:
                with print_lock:
                    print(f"  [FAIL] Platform worker crashed: {e}")
                results[f"unknown_{len(results)}"] = {"success": False, "error": str(e)}

    # Keep a stable summary order
    ordered: dict[str, dict] = {}
    for plat in ("youtube", "instagram", "tiktok"):
        if plat in results:
            ordered[plat] = results[plat]
    for plat, res in results.items():
        if plat not in ordered:
            ordered[plat] = res
    results = ordered

    if move_to_posted and any(outcome.get("success") for outcome in results.values()):
        os.makedirs("posted", exist_ok=True)
        dest = os.path.join("posted", os.path.basename(video_path))
        if os.path.abspath(video_path) != os.path.abspath(dest):
            try:
                import shutil
                shutil.move(video_path, dest)
                print(f"\n[OK] Moved published video to: {dest}")
                for outcome in results.values():
                    outcome["posted_file"] = dest
            except Exception as e:
                print(f"\n[Warning] Could not move video to posted/: {e}")

    print("\n" + "=" * 60)
    print("Summary:")
    for plat, outcome in results.items():
        st = "SUCCESS" if outcome.get("success") else "FAILED"
        url = outcome.get("url", outcome.get("error", ""))
        print(f"  - {plat.capitalize():<12}: {st} ({url})")
    print("=" * 60 + "\n")

    return results


def run_setup_wizard():
    """Interactive wizard to guide user in setting up YouTube, Instagram, and TikTok."""
    print("\n" + "=" * 60)
    print(">> Social Media Auto-Post Setup Wizard")
    print("=" * 60)
    print("This wizard will help you configure accounts for auto-publishing.\n")

    cfg = load_config()

    # 1. Instagram
    print("1. Instagram Configuration:")
    cur_user = cfg["instagram"].get("username", "")
    new_user = input(f"   Instagram Username [{cur_user}]: ").strip() or cur_user
    if new_user:
        new_pass = input("   Instagram Password (leave blank to keep current): ").strip()
        cfg["instagram"]["username"] = new_user
        if new_pass:
            cfg["instagram"]["password"] = new_pass

    # 2. YouTube
    print("\n2. YouTube Configuration:")
    cur_secrets = cfg["youtube"].get("client_secrets_file", "client_secrets.json")
    print(f"   YouTube uses Google OAuth2. Place your 'client_secrets.json' in this directory.")
    new_secrets = input(f"   Secrets filename [{cur_secrets}]: ").strip() or cur_secrets
    cfg["youtube"]["client_secrets_file"] = new_secrets

    # 3. TikTok
    print("\n3. TikTok Configuration:")
    do_tt = input("   Would you like to log into TikTok now to save session? (y/n) [n]: ").strip().lower()
    if do_tt == "y":
        setup_tiktok_interactive(cfg["tiktok"].get("session_dir", "tiktok_session"))

    # Save to config.json
    with open("config.json", "w", encoding="utf-8") as f:
    # Save to clients.json (or config.json if already present)
    target_config = "clients.json" if os.path.isfile("clients.json") or not os.path.isfile("config.json") else "config.json"
    with open(target_config, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print("\n[OK] Configuration saved to config.json! (Protected by .gitignore)")
    print(f"\n[OK] Configuration saved to {target_config}! (Protected by .gitignore)")


def main():
    p = argparse.ArgumentParser(
        description="Auto-post video clips to YouTube Shorts, Instagram Reels, and TikTok."
    )
    p.add_argument("--video", help="Path to vertical MP4 video to upload")
    p.add_argument("--title", default="", help="Video title (used for YouTube and captions)")
    p.add_argument("--caption", default="", help="Custom caption / description text")
    p.add_argument("--tags", default="", help="Comma-separated hashtags (e.g. 'Shorts,DJ,EDM')")
    p.add_argument("--platforms", default="all", help="Target platforms: 'all', or comma-separated 'youtube,instagram,tiktok'")
    p.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"], help="Privacy status (default: public)")
    p.add_argument("--config", default="config.json", help="Path to credentials config.json (default: config.json)")
    p.add_argument("--config", default="clients.json", help="Path to credentials clients.json (default: clients.json)")
    p.add_argument("--dry-run", action="store_true", help="Simulate upload without making live API calls")
    p.add_argument("--setup", action="store_true", help="Run interactive setup wizard to configure credentials")
    p.add_argument("--setup-youtube", action="store_true", help="Run Google OAuth and save YouTube token.json")
    p.add_argument("--setup-tiktok", action="store_true", help="Open browser to log into TikTok and save session")
    p.add_argument("--setup-instagram", action="store_true", help="Open browser to log into Instagram and save session")
    p.add_argument("--no-move", action="store_true", help="Do not move video to posted/ after upload")
    p.add_argument("--headed", action="store_true", help="Show browser windows during Instagram/TikTok uploads (useful for debugging)")
    args = p.parse_args()

    if args.setup:
        run_setup_wizard()
        return

    if args.setup_youtube:
        from connections import setup_youtube_interactive

        result = setup_youtube_interactive(args.config)
        if not result.get("success"):
            sys.exit(result.get("error") or "YouTube setup failed")
        print(result.get("message") or "[OK] YouTube connected.")
        return

    if args.setup_tiktok:
        cfg = load_config(args.config)
        setup_tiktok_interactive(cfg["tiktok"].get("session_dir", "tiktok_session"))
        return

    if args.setup_instagram:
        cfg = load_config(args.config)
        setup_instagram_interactive(
            session_file=cfg["instagram"].get("session_file", "instagram_session.json"),
            session_dir=cfg["instagram"].get("session_dir", "instagram_browser"),
        )
        return

    if not args.video:
        p.print_help()
        sys.exit("\nError: --video <path_to_video.mp4> is required.")

    tag_list = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    post_to_all(
        video_path=args.video,
        title=args.title,
        caption=args.caption,
        tags=tag_list,
        platforms=args.platforms,
        config_path=args.config,
        dry_run=args.dry_run,
        privacy=args.privacy,
        move_to_posted=not args.no_move,
        headless=not args.headed,
    )


if __name__ == "__main__":
    main()
