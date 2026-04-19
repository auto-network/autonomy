"""L2.B behavioral tests for /api/dao/recent_sessions per-type quotas (auto-wyo79).

Covers the end-to-end HTTP contract:

  * `type=all` (the default) returns a quota-mixed payload: at most 10
    dispatch + 10 librarian + 20 interactive, regardless of which group
    dominates the raw window.
  * `type=interactive` (chip-selected) funnels the whole budget into
    interactive rows, with no dispatch/librarian leakage.
  * `limit=` is deprecated — passing it does not expand the payload past
    the server-side quota cap.

The fixture seeds 30 dispatch + 30 librarian + 3 interactive rows so the
quota logic has to actively protect the interactive rows from being
shoved out of the top-N.
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest


def _iso(minutes_ago: int) -> str:
    """Recent ISO-8601 timestamp the DAO will admit under since=1d."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _make_recent_row(
    idx: int,
    session_type: str,
    minutes_ago: int,
) -> dict:
    return {
        "id": f"src-{session_type}-{idx:03d}",
        "type": "session",
        "title": f"{session_type.title()} {idx}",
        "project": "autonomy",
        "session_uuid": f"uuid-{session_type}-{idx}",
        "session_type": session_type,
        "last_activity_at": _iso(minutes_ago),
        "created_at": _iso(minutes_ago + 60),
        "ended_at": _iso(minutes_ago),
        "entry_count": 10 + idx,
        "total_turns": 10 + idx,
        "context_tokens": 1000 * idx,
        "total_tokens": 1000 * idx,
    }


def _quota_fixture() -> dict:
    recent = []
    # 30 dispatch rows, staggered within a few hours (all within 1d)
    for i in range(30):
        recent.append(_make_recent_row(i, "dispatch", minutes_ago=1 + i * 5))
    # 30 librarian rows
    for i in range(30):
        recent.append(_make_recent_row(i, "librarian", minutes_ago=2 + i * 5))
    # 3 interactive rows
    for i in range(3):
        recent.append(_make_recent_row(i, "interactive", minutes_ago=30 + i * 10))
    return {
        "beads": [],
        "active_sessions": [],
        "session_entries": {},
        "recent_sessions": recent,
    }


@pytest.fixture
def quota_client(tmp_path, monkeypatch):
    """TestClient backed by a DASHBOARD_MOCK fixture with imbalanced types."""
    fixture_path = tmp_path / "fixtures.json"
    fixture_path.write_text(json.dumps(_quota_fixture(), indent=2))
    monkeypatch.setenv("DASHBOARD_MOCK", str(fixture_path))

    # Reload mock DAO + server so the new DASHBOARD_MOCK path is picked up.
    from tools.dashboard.dao import mock as mock_mod
    importlib.reload(mock_mod)
    from tools.dashboard import server
    importlib.reload(server)

    from starlette.testclient import TestClient
    with TestClient(server.app) as client:
        yield client


class TestTypeQuotasHTTP:
    """GET /api/dao/recent_sessions honours per-type quotas (auto-wyo79)."""

    def test_default_quotas_include_interactive(self, quota_client):
        """`type=all&since=1d` must surface all 3 interactive rows even
        when dispatch + librarian rows outnumber them 20:1."""
        resp = quota_client.get("/api/dao/recent_sessions?since=1d&type=all")
        assert resp.status_code == 200
        data = resp.json()
        by_type: dict[str, list[dict]] = {
            "interactive": [], "dispatch": [], "librarian": [],
        }
        for row in data:
            by_type.setdefault(row["session_type"], []).append(row)

        assert len(by_type["interactive"]) == 3, \
            f"expected 3 interactive rows; got {len(by_type['interactive'])}"
        assert len(by_type["dispatch"]) == 10, \
            f"expected dispatch capped at 10; got {len(by_type['dispatch'])}"
        assert len(by_type["librarian"]) == 10, \
            f"expected librarian capped at 10; got {len(by_type['librarian'])}"
        # Total cap 40; with 3 interactive + 10 dispatch + 10 librarian = 23
        assert len(data) <= 40, f"payload exceeded 40-row cap: {len(data)}"

    def test_interactive_chip_funnels_budget(self, quota_client):
        """`type=interactive` returns only interactive rows, up to 50."""
        resp = quota_client.get(
            "/api/dao/recent_sessions?since=1w&type=interactive"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3, f"expected 3 interactive rows; got {len(data)}"
        for row in data:
            assert row["session_type"] == "interactive", \
                f"non-interactive leaked: {row['session_type']}"

    def test_dispatch_chip_funnels_budget(self, quota_client):
        """`type=dispatch` funnels the budget into dispatch rows."""
        resp = quota_client.get(
            "/api/dao/recent_sessions?since=1w&type=dispatch"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(r["session_type"] == "dispatch" for r in data), \
            f"non-dispatch leaked: {[r['session_type'] for r in data]}"
        assert len(data) == 30, f"expected all 30 dispatch rows; got {len(data)}"

    def test_limit_param_is_ignored(self, quota_client):
        """`limit` is deprecated; passing it must not shrink or expand the payload."""
        no_limit = quota_client.get(
            "/api/dao/recent_sessions?since=1d&type=all"
        ).json()
        with_limit = quota_client.get(
            "/api/dao/recent_sessions?since=1d&type=all&limit=5"
        ).json()
        assert len(no_limit) == len(with_limit), \
            f"limit=5 changed row count ({len(no_limit)} vs {len(with_limit)})"

    def test_unknown_type_falls_back_to_all(self, quota_client):
        """Unknown `type` values use the 'all' quota table."""
        default = quota_client.get(
            "/api/dao/recent_sessions?since=1d&type=all"
        ).json()
        bogus = quota_client.get(
            "/api/dao/recent_sessions?since=1d&type=zzz"
        ).json()
        assert [r["id"] for r in default] == [r["id"] for r in bogus]

    def test_sort_applied_across_union(self, quota_client):
        """The merged union is sorted by the requested column, not per group."""
        resp = quota_client.get(
            "/api/dao/recent_sessions?since=1d&type=all&sort=turns"
        )
        data = resp.json()
        turn_counts = [
            (r.get("entry_count") or r.get("total_turns") or 0) for r in data
        ]
        assert turn_counts == sorted(turn_counts, reverse=True), \
            f"turns not monotonically decreasing: {turn_counts}"
