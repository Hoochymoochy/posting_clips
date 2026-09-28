"""
Colored logging via colorlog.

Under systemd/journald stdout is not a TTY, so colors are forced when
INVOCATION_ID is set (systemd) or FORCE_COLOR=1. Disable with NO_COLOR=1.

View ANSI colors in the journal with:
  journalctl -u <service> -f -o cat
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import sys
from typing import Any

_CONFIGURED = False
_PRINT_WRAPPED = False
_ORIG_PRINT = builtins.print

# Tag → colorlog color name (applied to the whole print line)
_TAG_COLORS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[FAIL\]", re.I), "red"),
    (re.compile(r"\[WARN\]", re.I), "yellow"),
    (re.compile(r"\[OK\]", re.I), "green"),
    (re.compile(r"\[RETRY\]", re.I), "cyan"),
    (re.compile(r"\[SKIP\]", re.I), "thin_white"),
    (re.compile(r"\[debug\]", re.I), "thin_cyan"),
    (re.compile(r"\[Server\]", re.I), "blue"),
    (re.compile(r"\[Worker\]", re.I), "blue"),
    (re.compile(r"\[Discovery\]", re.I), "purple"),
    (re.compile(r"\[DISCORD\]", re.I), "purple"),
    (re.compile(r"\[manual-retry\]", re.I), "cyan"),
    (re.compile(r"^>>\s"), "bold_white"),
]


def _want_color() -> bool:
    # Match colorlog: FORCE_COLOR wins over NO_COLOR
    if os.environ.get("FORCE_COLOR", "").lower() in ("1", "true", "yes"):
        return True
    # systemd sets this for every service process — force ANSI for journalctl -o cat
    if os.environ.get("INVOCATION_ID"):
        return True
    if os.environ.get("NO_COLOR", "").strip():
        return False
    return sys.stdout.isatty() or sys.stderr.isatty()


def setup_logging(level: str | None = None) -> None:
    """Configure root + common library loggers with colorlog."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    try:
        import colorlog
    except ImportError:
        logging.basicConfig(
            level=getattr(logging, (level or os.environ.get("LOG_LEVEL", "INFO")).upper(), logging.INFO),
            format="%(asctime)s %(levelname)-8s %(message)s",
            datefmt="%H:%M:%S",
        )
        _CONFIGURED = True
        return

    level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    force = _want_color()

    handler = colorlog.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        colorlog.ColoredFormatter(
            "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(message)s",
            datefmt="%H:%M:%S",
            log_colors={
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
            stream=sys.stdout,
            force_color=force,
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))

    # Keep noisy libs quieter unless LOG_LEVEL=DEBUG
    if level_name != "DEBUG":
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.INFO)

    _CONFIGURED = True


def get_logger(name: str = "posting_clips") -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def _colorize_message(msg: str) -> str:
    if not _want_color() or not msg:
        return msg
    try:
        from colorlog.escape_codes import escape_codes, parse_colors
    except ImportError:
        return msg

    reset = escape_codes.get("reset", "\033[0m")
    for pattern, color_name in _TAG_COLORS:
        if pattern.search(msg):
            try:
                prefix = parse_colors(color_name)
            except KeyError:
                prefix = escape_codes.get(color_name, "")
            if prefix:
                return f"{prefix}{msg}{reset}"
    return msg


def install_colored_print() -> None:
    """
    Wrap builtins.print so existing [WARN]/[FAIL]/[OK]/… lines get ANSI color.
    Safe under journald when FORCE_COLOR / INVOCATION_ID is set.
    """
    global _PRINT_WRAPPED
    if _PRINT_WRAPPED:
        return

    def colored_print(*args: Any, **kwargs: Any) -> None:
        file = kwargs.get("file", None)
        if file is not None and file not in (sys.stdout, sys.stderr):
            return _ORIG_PRINT(*args, **kwargs)

        sep = kwargs.get("sep", " ")
        msg = sep.join(str(a) for a in args)
        colored = _colorize_message(msg)
        if colored is msg:
            return _ORIG_PRINT(*args, **kwargs)

        # Re-print as a single colored string (preserve end/flush/file)
        kw = dict(kwargs)
        kw.pop("sep", None)
        return _ORIG_PRINT(colored, **kw)

    builtins.print = colored_print  # type: ignore[assignment]
    _PRINT_WRAPPED = True


def init_logging(level: str | None = None) -> logging.Logger:
    """One-shot: colorlog + colored print. Call once at process entry."""
    setup_logging(level)
    install_colored_print()
    return get_logger()
