"""Tests for discovery queue update (DJ-set feed → discovery_runs)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import uuid4
from urllib.error import HTTPError
from io import BytesIO

import pytest

from discovery.queue import enqueue_urls, extract_urls, fetch_dj_sets, update_queue


class _Result:
    def __init__(self, data: list[dict[str, Any]] | None = None, count: int | None = None):
        self.data = data if data is not None else []
        self.count = count if count is not None else len(self.data)


class _FakeQuery:
    def __init__(self, store: list[dict[str, Any]]):
        self._store = store
        self._filters: list[tuple[str, str, Any]] = []
        self._op: str | None = None
        self._payload: Any = None

    def select(self, *_args, **_kwargs):
        self._op = "select"
        return self

    def insert(self, row: dict[str, Any]):
        self._op = "insert"
        self._payload = row
        return self

    def in_(self, col: str, vals: list[Any]):
        self._filters.append(("in", col, vals))
        return self

    def eq(self, col: str, val: Any):
        self._filters.append(("eq", col, val))
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        for kind, col, val in self._filters:
            cell = row.get(col)
            if kind == "in":
                if cell not in val:
                    return False
            elif kind == "eq":
                if cell != val:
                    return False
        return True

    def execute(self) -> _Result:
        if self._op == "select":
            matched = [r for r in self._store if self._matches(r)]
            return _Result(data=matched)
        if self._op == "insert":
            now = datetime.now(timezone.utc).isoformat()
            row = {
                "id": str(uuid4()),
                "created_at": now,
                "updated_at": now,
                **self._payload,
            }
            self._store.append(row)
            return _Result(data=[row])
        raise RuntimeError(f"unsupported op: {self._op}")


class FakeSupabase:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = list(rows or [])

    def table(self, name: str) -> _FakeQuery:
        assert name == "discovery_runs"
        return _FakeQuery(self.rows)


SAMPLE_SET = {
    "video_id": "dQw4w9WgXcQ",
    "title": "Example DJ Set — Live",
    "channel": "Example Channel",
    "published_at": "2024-01-15T18:00:00Z",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "fetched_at": "2026-04-16T12:00:00Z",
    "view_count": 125000,
    "duration_seconds": 3720,
    "genres": ["house", "techno"],
    "contexts": ["club", "live"],
    "energy": "high",
    "location": None,
    "score": 87.5,
    "content_type": "authentic",
    "quality_issues": [],
}


def test_extract_urls_dedupes_and_skips_empty():
    sets = [
        SAMPLE_SET,
        {**SAMPLE_SET, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        {**SAMPLE_SET, "url": "  "},
        {**SAMPLE_SET, "url": "https://www.youtube.com/watch?v=abc123"},
        {"title": "no url"},
    ]
    assert extract_urls(sets) == [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=abc123",
    ]


def test_enqueue_urls_inserts_new_and_skips_existing():
    existing_url = "https://www.youtube.com/watch?v=already"
    new_url = "https://www.youtube.com/watch?v=new"
    sb = FakeSupabase(
        [
            {
                "id": str(uuid4()),
                "youtube_url": existing_url,
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )

    stats = enqueue_urls(sb, [existing_url, new_url])
    assert stats["fetched"] == 2
    assert stats["inserted"] == 1
    assert stats["skipped"] == 1
    assert stats["inserted_urls"] == [new_url]
    assert len(sb.rows) == 2
    assert sb.rows[-1]["status"] == "pending"
    assert sb.rows[-1]["youtube_url"] == new_url


def test_enqueue_urls_skips_even_if_prior_status_failed():
    url = "https://www.youtube.com/watch?v=failed-once"
    sb = FakeSupabase(
        [
            {
                "id": str(uuid4()),
                "youtube_url": url,
                "status": "failed",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )
    stats = enqueue_urls(sb, [url])
    assert stats["inserted"] == 0
    assert stats["skipped"] == 1
    assert len(sb.rows) == 1


def test_fetch_dj_sets_parses_list():
    payload = json.dumps([SAMPLE_SET]).encode("utf-8")

    class _Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("discovery.queue.urllib.request.urlopen", return_value=_Resp()):
        sets = fetch_dj_sets("https://example.com/dj-set")
    assert len(sets) == 1
    assert sets[0]["url"] == SAMPLE_SET["url"]


def test_fetch_dj_sets_parses_wrapped_list():
    payload = json.dumps({"sets": [SAMPLE_SET]}).encode("utf-8")

    class _Resp:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("discovery.queue.urllib.request.urlopen", return_value=_Resp()):
        sets = fetch_dj_sets("https://example.com/dj-set")
    assert len(sets) == 1


def test_fetch_dj_sets_http_error():
    err = HTTPError(
        url="https://example.com/dj-set",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=BytesIO(b"missing"),
    )
    with patch("discovery.queue.urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="HTTP 404"):
            fetch_dj_sets("https://example.com/dj-set")


def test_update_queue_end_to_end():
    sb = FakeSupabase()
    sets = [
        SAMPLE_SET,
        {**SAMPLE_SET, "url": "https://www.youtube.com/watch?v=second"},
    ]

    with patch("discovery.queue.fetch_dj_sets", return_value=sets):
        stats = update_queue(sb, feed_url="https://example.com/dj-set")

    assert stats["fetched"] == 2
    assert stats["inserted"] == 2
    assert stats["skipped"] == 0
    assert stats["feed_url"] == "https://example.com/dj-set"
    assert len(sb.rows) == 2

    with patch("discovery.queue.fetch_dj_sets", return_value=sets):
        stats2 = update_queue(sb, feed_url="https://example.com/dj-set")
    assert stats2["inserted"] == 0
    assert stats2["skipped"] == 2
    assert len(sb.rows) == 2
