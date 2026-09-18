#!/usr/bin/env python3
"""
export_env_credentials.py — Turn clients.json into .env credential vars.

Usage (from posting_clips/):
  python export_env_credentials.py           # print lines to stdout
  python export_env_credentials.py --write   # append/update posting_clips/.env

Cloud tip: paste the printed YOUTUBE_*_BASE64 / INSTAGRAM_SESSIONID into
your host's environment. On boot, load_config() materializes clients.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CLIENTS = ROOT / "clients.json"
ENV_PATH = ROOT / ".env"


def _b64(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _upsert_env(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    if pattern.search(text):
        return pattern.sub(line, text)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + line + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description="Export clients.json → .env credential vars")
    p.add_argument("--write", action="store_true", help=f"Update {ENV_PATH.name}")
    args = p.parse_args()

    if not CLIENTS.is_file():
        raise SystemExit(f"Missing {CLIENTS}")

    cfg = json.loads(CLIENTS.read_text(encoding="utf-8"))
    yt = cfg.get("youtube") or {}
    ig = cfg.get("instagram") or {}

    lines: list[tuple[str, str]] = []
    if isinstance(yt.get("client_secrets"), dict):
        lines.append(("YOUTUBE_CLIENT_SECRETS_JSON_BASE64", _b64(yt["client_secrets"])))
    if isinstance(yt.get("token"), dict):
        lines.append(("YOUTUBE_TOKEN_JSON_BASE64", _b64(yt["token"])))

    sid = ""
    auth = ig.get("authorization_data")
    if isinstance(auth, dict):
        sid = auth.get("sessionid") or ""
    sid = sid or ig.get("sessionid") or ""
    if sid:
        lines.append(("INSTAGRAM_SESSIONID", sid))

    if not lines:
        raise SystemExit("No youtube.client_secrets / youtube.token / instagram sessionid found.")

    block = "\n".join(f"{k}={v}" for k, v in lines)
    if args.write:
        existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.is_file() else ""
        for k, v in lines:
            existing = _upsert_env(existing, k, v)
        ENV_PATH.write_text(existing, encoding="utf-8")
        print(f"Updated {ENV_PATH} with: {', '.join(k for k, _ in lines)}")
    else:
        print("# Paste into remote posting_clips .env / host secrets:")
        print(block)


if __name__ == "__main__":
    main()
