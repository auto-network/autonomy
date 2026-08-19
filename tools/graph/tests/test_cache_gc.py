"""Integration tests for ``@cache(ttl=...)`` storage + ``graph maintenance cache-gc``.

Covers:

* ``add_setting`` on a ``@cache`` schema populates ``expires_at`` to
  ``updated_at + ttl``; non-``@cache`` schemas leave it ``NULL``.
* UPDATE paths (promote/deprecate/migrate) recompute ``expires_at``
  whenever they bump ``updated_at`` — TTL is sliding, not fixed at row
  creation.
* :func:`run_cache_gc` deletes elapsed ``raw``/``curated`` rows for
  ``@cache`` schemas, leaves ``NULL`` ``expires_at`` rows alone, and
  spares ``published``/``canonical`` rows with a WARNING log.
* ``--limit`` bounds a single sweep.
* ``--dry-run`` emits logs but DELETEs nothing.
* ``--org SLUG`` narrows the sweep to one DB.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from tools.graph import ops, org_ops, schemas, settings_ops
from tools.graph.maintenance.cache_gc import run_cache_gc, sweep_db
from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SettingSchema,
    cache,
    register_schema,
    singleton,
)


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file (legacy single-DB layout)."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture
def cache_schema():
    @cache(ttl=timedelta(days=30))
    class CacheV1(SettingSchema):
        set_id = "autonomy.test.cache"
        schema_revision = 1

    register_schema("autonomy.test.cache", 1, CacheV1)
    return CacheV1


@pytest.fixture
def non_cache_schema():
    """A non-cache schema for negative coverage on expires_at."""
    @singleton
    class V1(SettingSchema):
        set_id = "autonomy.test.solo"
        schema_revision = 1

    register_schema("autonomy.test.solo", 1, V1)
    return V1


# ── Helpers ──────────────────────────────────────────────────


def _read_row(db_path: Path, sid: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM settings WHERE id = ?", (sid,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else {}


def _all_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM settings").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _set_row_expires_at(db_path: Path, sid: str, expires_at: str | None) -> None:
    """Backdate a row's ``expires_at`` so the GC sees it as elapsed."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE settings SET expires_at = ? WHERE id = ?",
            (expires_at, sid),
        )
        conn.commit()
    finally:
        conn.close()


# ── Write-path expires_at population ─────────────────────────


def test_add_setting_on_cache_schema_populates_expires_at(
    graph_db_env, cache_schema,
):
    sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    row = _read_row(graph_db_env, sid)
    assert row["expires_at"] is not None
    # expires_at = updated_at + 30 days; both are ISO strings.
    assert row["expires_at"] > row["updated_at"]


def test_add_setting_on_non_cache_schema_leaves_expires_at_null(
    graph_db_env, non_cache_schema,
):
    sid = ops.add_setting("autonomy.test.solo", 1, "default", {"x": 1}, org=ops.CALLER_ORG)
    row = _read_row(graph_db_env, sid)
    assert row["expires_at"] is None


def test_promote_recomputes_expires_at(graph_db_env, cache_schema):
    sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    pre_row = _read_row(graph_db_env, sid)
    pre_expires = pre_row["expires_at"]
    # Promotion is a state change, but TTL is sliding — promote bumps
    # updated_at and therefore expires_at. Cache rows are unusual at
    # 'curated' state (the docs note this); the substrate doesn't
    # forbid it.
    ops.promote_setting(sid, "curated", org=ops.CALLER_ORG)
    post_row = _read_row(graph_db_env, sid)
    assert post_row["expires_at"] is not None
    # New expires_at must be >= old (time monotonically advances or
    # ties on a same-second clock; either is valid).
    assert post_row["expires_at"] >= pre_expires
    assert post_row["updated_at"] >= pre_row["updated_at"]


def test_deprecate_recomputes_expires_at(graph_db_env, cache_schema):
    sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    pre_row = _read_row(graph_db_env, sid)
    ops.deprecate_setting(sid, org=ops.CALLER_ORG)
    post_row = _read_row(graph_db_env, sid)
    assert post_row["expires_at"] is not None
    assert post_row["expires_at"] >= pre_row["expires_at"]
    assert post_row["deprecated"] == 1


def test_override_setting_inherits_cache_schema_expires_at(
    graph_db_env, cache_schema,
):
    base_sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    override_sid = ops.override_setting(base_sid, {"x": 2}, org=ops.CALLER_ORG)
    row = _read_row(graph_db_env, override_sid)
    assert row["expires_at"] is not None


def test_exclude_setting_inherits_cache_schema_expires_at(
    graph_db_env, cache_schema,
):
    base_sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    exclude_sid = ops.exclude_setting(base_sid, org=ops.CALLER_ORG)
    row = _read_row(graph_db_env, exclude_sid)
    assert row["expires_at"] is not None


# ── Sweep semantics ──────────────────────────────────────────


def test_sweep_db_deletes_elapsed_raw_cache_rows(graph_db_env, cache_schema):
    sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    # Backdate the expires_at to the past.
    _set_row_expires_at(graph_db_env, sid, "1970-01-01T00:00:00Z")

    # Sweep using a direct GraphDB handle (not the pool) so we can
    # observe the DB after the sweep. The CLI path uses the pool via
    # _iter_org_dbs; sweep_db itself is path-agnostic.
    from tools.graph.db import GraphDB
    db = GraphDB(str(graph_db_env))
    try:
        result = sweep_db(db, org="test", limit=10)
    finally:
        db.close()
    assert result.swept == 1
    assert result.by_set == {"autonomy.test.cache": 1}
    assert _read_row(graph_db_env, sid) == {}


def test_sweep_db_skips_non_expired_rows(graph_db_env, cache_schema):
    sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    # Default expires_at is 30 days in the future — should not be swept.
    from tools.graph.db import GraphDB
    db = GraphDB(str(graph_db_env))
    try:
        result = sweep_db(db, org="test", limit=10)
    finally:
        db.close()
    assert result.swept == 0
    assert _read_row(graph_db_env, sid) != {}


def test_sweep_db_skips_null_expires_at(graph_db_env, non_cache_schema):
    """Non-cache rows have NULL expires_at and must never be swept."""
    sid = ops.add_setting("autonomy.test.solo", 1, "default", {"x": 1}, org=ops.CALLER_ORG)
    from tools.graph.db import GraphDB
    db = GraphDB(str(graph_db_env))
    try:
        result = sweep_db(db, org="test", limit=10)
    finally:
        db.close()
    assert result.swept == 0
    assert _read_row(graph_db_env, sid) != {}


def test_sweep_db_spares_published_with_warning(
    graph_db_env, cache_schema, caplog,
):
    """Published cache rows shouldn't exist (cache rows stay raw), but if
    a state-promotion bug ever produced one, the GC must spare it AND
    surface a WARNING so the upstream bug doesn't silently lose data.
    """
    sid = ops.add_setting(
        "autonomy.test.cache", 1, "k", {"x": 1}, state="raw",
     org=ops.CALLER_ORG)
    # Promote past the GC's reach.
    ops.promote_setting(sid, "curated", org=ops.CALLER_ORG)
    ops.promote_setting(sid, "published", org=ops.CALLER_ORG)
    # Backdate so expires_at < now.
    _set_row_expires_at(graph_db_env, sid, "1970-01-01T00:00:00Z")

    from tools.graph.db import GraphDB
    db = GraphDB(str(graph_db_env))
    try:
        with caplog.at_level(logging.WARNING, logger="graph.cache_gc"):
            result = sweep_db(db, org="test", limit=10)
    finally:
        db.close()

    assert result.swept == 0
    assert result.skipped_published == 1
    assert _read_row(graph_db_env, sid) != {}, "published row must survive"
    assert any(
        "published cache row skipped" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    )


def test_sweep_db_respects_limit(graph_db_env, cache_schema):
    sids = [
        ops.add_setting("autonomy.test.cache", 1, f"k{i}", {"i": i}, org=ops.CALLER_ORG)
        for i in range(5)
    ]
    for sid in sids:
        _set_row_expires_at(graph_db_env, sid, "1970-01-01T00:00:00Z")

    from tools.graph.db import GraphDB
    db = GraphDB(str(graph_db_env))
    try:
        result = sweep_db(db, org="test", limit=2)
    finally:
        db.close()
    assert result.swept == 2
    # Three rows must remain.
    remaining = [
        r for r in _all_rows(graph_db_env)
        if r["set_id"] == "autonomy.test.cache"
    ]
    assert len(remaining) == 3


def test_sweep_db_dry_run_logs_but_does_not_delete(
    graph_db_env, cache_schema, caplog,
):
    sid = ops.add_setting("autonomy.test.cache", 1, "k", {"x": 1}, org=ops.CALLER_ORG)
    _set_row_expires_at(graph_db_env, sid, "1970-01-01T00:00:00Z")

    from tools.graph.db import GraphDB
    db = GraphDB(str(graph_db_env))
    try:
        with caplog.at_level(logging.INFO, logger="graph.cache_gc"):
            result = sweep_db(db, org="test", limit=10, dry_run=True)
    finally:
        db.close()
    assert result.swept == 1  # would-have-been
    assert _read_row(graph_db_env, sid) != {}, "dry-run must not delete"
    # Per-row log + summary log fire even in dry-run.
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("ttl_expired" in m for m in messages)
    assert any("sweep_summary" in m for m in messages)


def test_sweep_db_emits_structured_per_row_log(
    graph_db_env, cache_schema, caplog,
):
    sid = ops.add_setting("autonomy.test.cache", 1, "the-key", {"x": 1}, org=ops.CALLER_ORG)
    _set_row_expires_at(graph_db_env, sid, "1970-01-01T00:00:00Z")

    from tools.graph.db import GraphDB
    db = GraphDB(str(graph_db_env))
    try:
        with caplog.at_level(logging.INFO, logger="graph.cache_gc"):
            sweep_db(db, org="myorg", limit=10)
    finally:
        db.close()

    # Find the per-row record and parse its JSON payload.
    rows_rec = next(
        rec for rec in caplog.records
        if "ttl_expired" in rec.getMessage()
        and "sweep_summary" not in rec.getMessage()
    )
    # The format string is "ttl_expired %s" and the JSON is the arg.
    payload = json.loads(rows_rec.getMessage().split(" ", 1)[1])
    assert payload["set_id"] == "autonomy.test.cache"
    assert payload["key"] == "the-key"
    assert payload["org"] == "myorg"
    assert payload["reason"] == "ttl_expired"


# ── run_cache_gc / multi-org ─────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Per-org DB layout under a fresh ``data/orgs`` root."""
    root = tmp_path / "data" / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    return root


def _create_org_db(root: Path, slug: str) -> Path:
    """Create a fresh per-org DB without the bootstrap identity seed.

    We bypass ``ensure_bootstrap_orgs`` because that path requires the
    real ``autonomy.org#1`` schema and seeds rows we don't care about
    here. Direct DB creation gives us a clean settings table.
    """
    from tools.graph.db import GraphDB
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / f"{slug}.db"
    db = GraphDB(str(db_path))
    try:
        db.conn.execute(
            "INSERT INTO orgs(id, slug, type) VALUES(?, ?, ?)",
            (slug + "-id", slug, "shared"),
        )
        db.conn.commit()
    finally:
        db.close()
    return db_path


def _add_cache_row_directly(
    db_path: Path,
    set_id: str,
    schema_revision: int,
    key: str,
    *,
    state: str = "raw",
    expires_at: str | None = None,
) -> str:
    """Insert a settings row bypassing the ops layer.

    The cache_gc multi-org tests need rows in specific per-org DBs
    without going through the global ``add_setting`` (which routes via
    GRAPH_ORG / GRAPH_DB). We craft them by hand.
    """
    from uuid import uuid4
    sid = str(uuid4())
    now = "2026-01-01T00:00:00Z"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, set_id, int(schema_revision), key, "{}", state,
             now, now, expires_at),
        )
        conn.commit()
    finally:
        conn.close()
    return sid


def test_run_cache_gc_sweeps_across_orgs(orgs_root, cache_schema):
    a_path = _create_org_db(orgs_root, "alpha")
    b_path = _create_org_db(orgs_root, "beta")
    a_sid = _add_cache_row_directly(
        a_path, "autonomy.test.cache", 1, "ka",
        expires_at="1970-01-01T00:00:00Z",
    )
    b_sid = _add_cache_row_directly(
        b_path, "autonomy.test.cache", 1, "kb",
        expires_at="1970-01-01T00:00:00Z",
    )
    report = run_cache_gc(limit=10)
    assert report.swept == 2
    assert _read_row(a_path, a_sid) == {}
    assert _read_row(b_path, b_sid) == {}


def test_run_cache_gc_org_filter_narrows_to_one_db(orgs_root, cache_schema):
    a_path = _create_org_db(orgs_root, "alpha")
    b_path = _create_org_db(orgs_root, "beta")
    a_sid = _add_cache_row_directly(
        a_path, "autonomy.test.cache", 1, "ka",
        expires_at="1970-01-01T00:00:00Z",
    )
    b_sid = _add_cache_row_directly(
        b_path, "autonomy.test.cache", 1, "kb",
        expires_at="1970-01-01T00:00:00Z",
    )
    report = run_cache_gc(org="alpha", limit=10)
    assert report.swept == 1
    assert _read_row(a_path, a_sid) == {}
    assert _read_row(b_path, b_sid) != {}, "beta must be untouched"


def test_run_cache_gc_unknown_org_raises(orgs_root, cache_schema):
    with pytest.raises(ValueError, match="unknown org"):
        run_cache_gc(org="ghost", limit=10)


def test_run_cache_gc_sweeps_the_personal_store(tmp_path, monkeypatch, cache_schema):
    """The sweep enumerates every settings-bearing store, not only
    organizations: personal-homed cache schemas exist
    (claude_setup_tokens), and an orgs-only enumeration lets their expired
    rows accumulate forever (auto-35kmy Codex P1b)."""
    from tools.graph.db import GraphDB
    from tools.graph.maintenance.cache_gc import run_cache_gc

    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("acme").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    try:
        personal = GraphDB.for_org("personal", mode="rw")
        personal.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at, expires_at) "
            "VALUES('expired-tok','autonomy.test.cache',1,'tok','{}','raw',"
            "'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')"
        )
        personal.conn.commit()

        report = run_cache_gc()

        assert "personal" in report.by_org, "personal store absent from sweep"
        assert report.swept >= 1
        remaining = personal.conn.execute(
            "SELECT COUNT(*) FROM settings WHERE id = 'expired-tok'"
        ).fetchone()[0]
        assert remaining == 0, "the personal store's expired cache row survives"
    finally:
        GraphDB.close_all_pooled()
