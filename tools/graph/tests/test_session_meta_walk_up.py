"""``_load_session_meta`` walks up nested layouts; ingest fails closed.

Two cooperating bugs caused autonomy sessions to land in personal.db:

* Codex rollouts live three directories below their meta
  (``<run>/sessions/YYYY/MM/DD/rollout-*.jsonl`` vs.
  ``<run>/sessions/.session_meta.json``). The old two-level walk
  could not see the meta, so ``session_target_org`` fell back to
  ``personal``.

* When meta was unreachable for any reason, the routing helper
  returned ``personal`` instead of skipping. The dedup check is
  per-DB, so today's re-ingest pass happily wrote a second row for
  every autonomy session it couldn't classify.

These tests pin the new behaviour: walk up far enough to find the
meta, and skip rather than route to personal when the org cannot be
resolved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph import db as graph_db_mod
from tools.graph.db import GraphDB
from tools.graph.ingest import (
    _ingest_session_routed,
    _load_session_meta,
    _open_db_for_session,
    session_target_org,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin orgs dir + clear ``GRAPH_DB``/``GRAPH_ORG`` for routing tests."""
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    return root


def _write_meta(dir_path: Path, meta: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / ".session_meta.json"
    p.write_text(json.dumps(meta))
    return p


# ── Walk-up: codex layout (three dirs below meta) ─────────────────


def test_load_session_meta_finds_meta_three_levels_up(tmp_path):
    """Codex rollout layout: meta at ``sessions/.session_meta.json``,
    JSONL at ``sessions/YYYY/MM/DD/rollout-*.jsonl``."""
    sessions = tmp_path / "agent-runs" / "auto-x-1" / "sessions"
    _write_meta(sessions, {"graph_org": "autonomy", "harness": "codex"})

    rollout_dir = sessions / "2026" / "04" / "28"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / "rollout-2026-04-28T00-00-00-abc.jsonl"
    rollout.touch()

    meta = _load_session_meta(rollout)
    assert meta.get("graph_org") == "autonomy"
    assert session_target_org(rollout) == "autonomy"


def test_load_session_meta_finds_meta_two_levels_up(tmp_path):
    """Backward-compat with the old two-level walk shape."""
    sessions = tmp_path / "agent-runs" / "auto-y" / "sessions"
    _write_meta(sessions, {"graph_org": "autonomy"})

    project_dir = sessions / "-workspace-repo"
    project_dir.mkdir()
    jsonl = project_dir / "uuid-1.jsonl"
    jsonl.touch()

    assert session_target_org(jsonl) == "autonomy"


def test_load_session_meta_finds_meta_in_same_dir(tmp_path):
    sessions = tmp_path / "sessions"
    _write_meta(sessions, {"graph_org": "anchore"})
    jsonl = sessions / "abc.jsonl"
    jsonl.touch()

    assert session_target_org(jsonl) == "anchore"


def test_load_session_meta_deepest_meta_wins(tmp_path):
    """When two metas exist on the path up, the closer one wins.

    The rule keeps a per-session meta inside a project subdirectory
    authoritative even if a stray meta sits higher up.
    """
    outer = tmp_path / "agent-runs" / "auto-z" / "sessions"
    _write_meta(outer, {"graph_org": "personal"})  # higher, should lose

    inner = outer / "nested"
    _write_meta(inner, {"graph_org": "autonomy"})  # closer, should win
    jsonl = inner / "uuid.jsonl"
    jsonl.touch()

    assert session_target_org(jsonl) == "autonomy"


def test_load_session_meta_stops_at_agent_runs_boundary(tmp_path):
    """The walk does not escape the run directory tree.

    If we kept walking past ``agent-runs`` we'd pick up unrelated
    metas from a sibling run (or worse, repo-level config).
    """
    runs_root = tmp_path / "agent-runs"
    runs_root.mkdir()
    # A poisoned meta one level above agent-runs: must NOT be read.
    _write_meta(tmp_path, {"graph_org": "should-not-pick-this"})

    sessions = runs_root / "auto-q" / "sessions"
    sessions.mkdir(parents=True)
    deep = sessions / "2026" / "04" / "28"
    deep.mkdir(parents=True)
    rollout = deep / "rollout.jsonl"
    rollout.touch()

    # No meta inside the run, walk hits ``agent-runs`` and stops.
    assert _load_session_meta(rollout) == {}
    assert session_target_org(rollout) is None


def test_load_session_meta_returns_empty_when_nothing_found(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    jsonl = deep / "x.jsonl"
    jsonl.touch()

    assert _load_session_meta(jsonl) == {}


# ── Fail-closed: no meta → skip, never route to personal ──────────


def test_ingest_session_routed_skips_when_meta_missing(orgs_root, tmp_path):
    """Fail-closed: no meta → ``status=skipped``, no row written.

    The skip prevents the cross-org duplicate pattern: a session
    routed correctly at session-end gets a second row in personal.db
    on a later sweep that couldn't find the meta.
    """
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db("autonomy").close()

    sessions = tmp_path / "agent-runs" / "auto-skip" / "sessions"
    sessions.mkdir(parents=True)
    jsonl = sessions / "no-meta.jsonl"
    jsonl.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')

    result = _ingest_session_routed(jsonl, force=False)
    assert result["status"] == "skipped"
    assert "no graph_org" in result["reason"]


def test_ingest_session_routed_skips_when_meta_lacks_graph_org(orgs_root, tmp_path):
    """Meta exists but contains neither ``graph_org`` nor ``graph_project``."""
    GraphDB.create_org_db("personal", type_="personal").close()

    sessions = tmp_path / "agent-runs" / "auto-thin" / "sessions"
    _write_meta(sessions, {"type": "dispatch", "container_name": "agent-thin"})
    jsonl = sessions / "x.jsonl"
    jsonl.write_text("")

    result = _ingest_session_routed(jsonl, force=False)
    assert result["status"] == "skipped"


def test_ingest_session_routed_routes_when_codex_meta_three_dirs_up(
    orgs_root, tmp_path,
):
    """End-to-end: codex layout routes to the meta's graph_org once
    the walk-up can see the meta."""
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    sessions = tmp_path / "agent-runs" / "auto-codex" / "sessions"
    _write_meta(sessions, {"graph_org": "autonomy", "harness": "codex"})

    rollout_dir = sessions / "2026" / "04" / "28"
    rollout_dir.mkdir(parents=True)
    rollout = rollout_dir / "rollout-x.jsonl"
    rollout.write_text("")

    # Empty rollout → parser produces no turns, but we should see the
    # session route through to autonomy.db (skipped="no content turns",
    # not skipped="no graph_org in meta").
    db = _open_db_for_session(rollout)
    assert db is not None
    try:
        assert Path(db.db_path) == orgs_root / "autonomy.db"
    finally:
        db.close()


def test_ingest_session_routed_legacy_graph_project_field(orgs_root, tmp_path):
    """Pre-rename meta uses ``graph_project`` instead of ``graph_org`` —
    the back-compat read path still routes correctly."""
    GraphDB.create_org_db("anchore").close()

    sessions = tmp_path / "agent-runs" / "auto-legacy" / "sessions"
    _write_meta(sessions, {"graph_project": "anchore"})
    jsonl = sessions / "x.jsonl"
    jsonl.touch()

    db = _open_db_for_session(jsonl)
    assert db is not None
    try:
        assert Path(db.db_path) == orgs_root / "anchore.db"
    finally:
        db.close()
