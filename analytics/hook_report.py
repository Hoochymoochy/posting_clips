#!/usr/bin/env python3
"""CLI: print per-archetype hook performance stats.

Usage (from posting_clips/):
  python -m analytics.hook_report
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    from hook_archetypes import build_hook_report

    try:
        report = build_hook_report()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
