"""Minimal tests for discovery run daily-cap tracking."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from discovery.limiter import (
    MAX_RUNS_PER_DAY,
    RunBlockedError,
    can_create_run,
    create_run,
)


class _Result:
    def __init__(self, data: list[dict[str, Any]] | None = None, count: int | None = None):
        self.data = data if data is not None else []
        self.count = count if count is not None else len(self.data)


class _FakeQuery:
    """Minimal PostgREST-style chain that filters an in-memory row list."""

    def __init__(self, store: list[dict[str, Any]]):
        self._store = store
        self._filters: list[tuple[str, str, Any]] = []
        self._op: str | None = None
        self._payload: Any = None
        self._count_exact = False
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    def select(self, *_args, count: str | None = None, **_kwargs):
        self._op = "select"
        self._count_exact = count == "exact"
        return self

    def insert(self, row: dict[str, Any]):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, row: dict[str, Any]):
        self._op = "update"
        self._payload = row
        return self

    def gte(self, col: str, val: Any):
        self._filters.append(("gte", col, val))
        return self

    def in_(self, col: str, vals: list[Any]):
        self._filters.append(("in", col, vals))
        return self

    def eq(self, col: str, val: Any):
        self._filters.append(("eq", col, val))
        return self

    def order(self, col: str, desc: bool = False):
        self._order = (col, desc)
        return self

    def limit(self, n: int):
        self._limit = int(n)
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        for kind, col, val in self._filters:
            cell = row.get(col)
            if kind == "gte":
                if str(cell) < str(val):
                    return False
            elif kind == "in":
                if cell not in val:
                    return False
            elif kind == "eq":
                if cell != val:
                    return False
        return True

    def execute(self) -> _Result:
        if self._op == "select":
            matched = [r for r in self._store if self._matches(r)]
            if self._order:
                col, desc = self._order
                matched.sort(key=lambda r: str(r.get(col) or ""), reverse=desc)
            if self._limit is not None:
                matched = matched[: self._limit]
            return _Result(data=matched, count=len(matched) if self._count_exact else None)
        if self._op == "insert":
            row = {
                "id": str(uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                **self._payload,
            }
            self._store.append(row)
            return _Result(data=[row])
        if self._op == "update":
            updated = []
            for row in self._store:
                if self._matches(row):
                    row.update(self._payload)
                    updated.append(row)
            return _Result(data=updated)
        raise RuntimeError(f"unsupported op: {self._op}")


class FakeSupabase:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = list(rows or [])

    def table(self, name: str) -> _FakeQuery:
        assert name == "discovery_runs"
        return _FakeQuery(self.rows)


def _row(
    *,
    status: str,
    created_at: datetime,
    youtube_url: str = "https://youtube.com/watch?v=abc",
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "youtube_url": youtube_url,
        "status": status,
        "created_at": created_at.isoformat(),
        "updated_at": created_at.isoformat(),
    }


def test_can_create_run_under_cap():
    now = datetime.now(timezone.utc)
    sb = FakeSupabase(
        [
            _row(status="pending", created_at=now),
            _row(status="success", created_at=now),
        ]
    )
    ok, reason = can_create_run(sb)
    assert ok is True
    assert reason is None


def test_can_create_run_blocked_at_cap():
    now = datetime.now(timezone.utc)
    sb = FakeSupabase(
        [
            _row(status="success", created_at=now, youtube_url=f"https://youtube.com/watch?v={i}")
            for i in range(MAX_RUNS_PER_DAY)
        ]
    )
    ok, reason = can_create_run(sb, "https://youtube.com/watch?v=new")
    assert ok is False
    assert reason == "daily_cap_reached"

    with pytest.raises(RunBlockedError) as exc_info:
        create_run(sb, "https://youtube.com/watch?v=new")
    assert exc_info.value.reason == "daily_cap_reached"


def test_previous_day_runs_do_not_count():
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    url = "https://youtube.com/watch?v=same"
    sb = FakeSupabase(
        [_row(status="success", created_at=yesterday, youtube_url=url) for _ in range(MAX_RUNS_PER_DAY)]
    )
    ok, reason = can_create_run(sb, url)
    assert ok is True
    assert reason is None


def test_failed_runs_do_not_count():
    now = datetime.now(timezone.utc)
    url = "https://youtube.com/watch?v=retry"
    sb = FakeSupabase(
        [
            *[_row(status="failed", created_at=now, youtube_url=url) for _ in range(MAX_RUNS_PER_DAY)],
            _row(status="pending", created_at=now, youtube_url="https://youtube.com/watch?v=other1"),
            _row(status="success", created_at=now, youtube_url="https://youtube.com/watch?v=other2"),
        ]
    )
    ok, reason = can_create_run(sb, url)
    assert ok is True
    assert reason is None


def test_same_url_blocked_once_per_day():
    now = datetime.now(timezone.utc)
    url = "https://youtube.com/watch?v=daily"
    sb = FakeSupabase([_row(status="success", created_at=now, youtube_url=url)])

    ok, reason = can_create_run(sb, url)
    assert ok is False
    assert reason == "url_already_ran_today"

    with pytest.raises(RunBlockedError) as exc_info:
        create_run(sb, url)
    assert exc_info.value.reason == "url_already_ran_today"


def test_same_url_allowed_after_failure_same_day():
    now = datetime.now(timezone.utc)
    url = "https://youtube.com/watch?v=retry-me"
    sb = FakeSupabase([_row(status="failed", created_at=now, youtube_url=url)])

    ok, reason = can_create_run(sb, url)
    assert ok is True
    assert reason is None

    run = create_run(sb, url)
    assert run["status"] == "pending"
    assert run["youtube_url"] == url


def test_claim_next_pending_run_sets_processing():
    from discovery.limiter import claim_next_pending_run

    now = datetime.now(timezone.utc)
    older = now - timedelta(minutes=5)
    sb = FakeSupabase(
        [
            _row(status="pending", created_at=now, youtube_url="https://youtube.com/watch?v=new"),
            _row(status="pending", created_at=older, youtube_url="https://youtube.com/watch?v=old"),
        ]
    )
    claimed = claim_next_pending_run(sb)
    assert claimed is not None
    assert claimed["status"] == "processing"
    assert claimed["youtube_url"].endswith("old")


def test_claim_respects_daily_active_cap():
    from discovery.limiter import MAX_RUNS_PER_DAY, claim_next_pending_run

    now = datetime.now(timezone.utc)
    rows = [
        _row(status="success", created_at=now, youtube_url=f"https://youtube.com/watch?v=s{i}")
        for i in range(MAX_RUNS_PER_DAY)
    ]
    rows.append(_row(status="pending", created_at=now, youtube_url="https://youtube.com/watch?v=wait"))
    sb = FakeSupabase(rows)
    assert claim_next_pending_run(sb) is None
