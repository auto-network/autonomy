"""Tests for the bulk promotion runner and ops.promote_source."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph import ops
from tools.graph.curation import promote
from tools.graph.curation.allowlist import load as load_allowlist
from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


def _insert_note(db, *, id: str, title: str, tags=None, state: str = "raw") -> Source:
    src = Source(
        id=id,
        type="note",
        platform="local",
        project=None,
        title=title,
        file_path=f"note:{id}",
        metadata={"tags": tags or [], "author": "librarian"},
        publication_state=state,
    )
    db.insert_source(src)
    db.insert_thought(Thought(source_id=src.id, content=f"body of {title}", role="user",
                              turn_number=1))
    db.insert_note_version(src.id, 1, f"body of {title}")
    return src


def _write_allowlist(path: Path, canonical, published) -> Path:
    body = "org: autonomy\nversion: 1\ncanonical:\n"
    for p in canonical:
        body += f"  - {p}\n"
    body += "published:\n"
    for p in published:
        body += f"  - {p}\n"
    path.write_text(body)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


# ── ops.promote_source primitive ─────────────────────────────


def test_promote_source_transitions_state(env):
    db = GraphDB(env)
    try:
        src = _insert_note(db, id="aaa", title="x")
    finally:
        db.close()
    rec = ops.promote_source("aaa", "canonical")
    assert rec["changed"] is True
    assert rec["prev_state"] == "raw"
    assert rec["new_state"] == "canonical"
    db = GraphDB(env)
    try:
        row = db.conn.execute("SELECT publication_state FROM sources WHERE id=?", ("aaa",)).fetchone()
        assert row["publication_state"] == "canonical"
    finally:
        db.close()


def test_promote_source_idempotent(env):
    db = GraphDB(env)
    try:
        _insert_note(db, id="aaa", title="x", state="canonical")
    finally:
        db.close()
    rec = ops.promote_source("aaa", "canonical")
    assert rec["changed"] is False


def test_promote_source_rejects_invalid_state(env):
    db = GraphDB(env)
    try:
        _insert_note(db, id="aaa", title="x")
    finally:
        db.close()
    with pytest.raises(ValueError, match="invalid publication_state"):
        ops.promote_source("aaa", "bogus")


def test_promote_source_rejects_unknown_id(env):
    GraphDB(env).close()
    with pytest.raises(LookupError):
        ops.promote_source("nosuch", "canonical")


# ── plan building ────────────────────────────────────────────


def test_build_plan_marks_ok(env, tmp_path):
    db = GraphDB(env)
    try:
        _insert_note(db, id="aaa", title="A", tags=["signpost"])
        _insert_note(db, id="bbb", title="B", tags=["architecture"])
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], ["bbb"])
        plan = promote.build_plan(db=db, allowlist=load_allowlist(allow))
    finally:
        db.close()
    statuses = {e.prefix: e.status for e in plan.entries}
    assert statuses == {"aaa": "ok", "bbb": "ok"}


def test_build_plan_blocks_on_pending_comments_for_canonical(env, tmp_path):
    db = GraphDB(env)
    try:
        src = _insert_note(db, id="aaa", title="A", tags=["signpost"])
        db.insert_comment(src.id, "unintegrated")
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], [])
        plan = promote.build_plan(db=db, allowlist=load_allowlist(allow))
    finally:
        db.close()
    entry = next(e for e in plan.entries if e.prefix == "aaa")
    assert entry.status == "blocked-comments"
    assert entry.pending_comments == 1


def test_build_plan_allows_published_with_pending_comments(env, tmp_path):
    """Published tier isn't gated on comment integration — only canonical is."""
    db = GraphDB(env)
    try:
        src = _insert_note(db, id="aaa", title="A", tags=["architecture"])
        db.insert_comment(src.id, "unintegrated")
        allow = _write_allowlist(tmp_path / "a.yaml", [], ["aaa"])
        plan = promote.build_plan(db=db, allowlist=load_allowlist(allow))
    finally:
        db.close()
    entry = next(e for e in plan.entries if e.prefix == "aaa")
    assert entry.status == "ok"


def test_build_plan_flags_missing_and_ambiguous(env, tmp_path):
    db = GraphDB(env)
    try:
        _insert_note(db, id="dup-aaa", title="A", tags=[])
        _insert_note(db, id="dup-bbb", title="B", tags=[])
        allow = _write_allowlist(tmp_path / "a.yaml", ["nope-00", "dup-"], [])
        plan = promote.build_plan(db=db, allowlist=load_allowlist(allow))
    finally:
        db.close()
    by_prefix = {e.prefix: e.status for e in plan.entries}
    assert by_prefix["nope-00"] == "missing"
    assert by_prefix["dup-"] == "ambiguous"


def test_build_plan_marks_already_at_target(env, tmp_path):
    db = GraphDB(env)
    try:
        _insert_note(db, id="aaa", title="A", tags=[], state="canonical")
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], [])
        plan = promote.build_plan(db=db, allowlist=load_allowlist(allow))
    finally:
        db.close()
    assert plan.entries[0].status == "already-at-target"


# ── execution ────────────────────────────────────────────────


def test_execute_applies_transitions_and_files_audit_note(env, tmp_path):
    db = GraphDB(env)
    try:
        _insert_note(db, id="aaa", title="A", tags=["signpost"])
        _insert_note(db, id="bbb", title="B", tags=["architecture"])
        _insert_note(db, id="ccc", title="C", tags=[], state="published")
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], ["bbb", "ccc"])
        plan = promote.build_plan(db=db, allowlist=load_allowlist(allow))
    finally:
        db.close()
    result = promote.execute(
        plan=plan, caller_org=None,
        actor="test", db_path=str(env),
    )

    # aaa: raw→canonical, bbb: raw→published, ccc: already-at-target (skipped)
    by_id = {t.source_id: t for t in result.transitions}
    assert by_id["aaa"].new_state == "canonical"
    assert by_id["aaa"].prev_state == "raw"
    assert by_id["bbb"].new_state == "published"
    assert "ccc" not in by_id
    assert len(result.skipped_already_at_target) == 1
    assert result.audit_note_id is not None

    db = GraphDB(env)
    try:
        # Audit note persisted, canonical, tagged for curation, body contains JSON block.
        row = db.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (result.audit_note_id,)
        ).fetchone()
        assert row is not None
        assert row["publication_state"] == "canonical"
        assert row["type"] == "note"
        tbody = db.conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ?", (result.audit_note_id,)
        ).fetchone()["content"]
        assert "Bootstrap allowlist promotion audit" in tbody
        assert "\"transitions\":" in tbody
    finally:
        db.close()


def test_execute_refuses_with_blockers(env, tmp_path):
    db = GraphDB(env)
    try:
        src = _insert_note(db, id="aaa", title="A", tags=["signpost"])
        db.insert_comment(src.id, "pending")
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], [])
        plan = promote.build_plan(db=db, allowlist=load_allowlist(allow))
    finally:
        db.close()
    with pytest.raises(promote.PromotionBlocked):
        promote.execute(
            plan=plan, caller_org=None,
            actor="test", db_path=str(env),
        )
    db = GraphDB(env)
    try:
        row = db.conn.execute(
            "SELECT publication_state FROM sources WHERE id=?", ("aaa",)
        ).fetchone()
        assert row["publication_state"] == "raw"
    finally:
        db.close()


# ── CLI shim ─────────────────────────────────────────────────


def test_cli_dry_run_does_not_mutate(env, tmp_path, capsys):
    db = GraphDB(env)
    try:
        _insert_note(db, id="aaa", title="A", tags=["signpost"])
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], [])
    finally:
        db.close()
    rc = promote.run(["--allowlist", str(allow), "--dry-run"])
    assert rc == 0
    db = GraphDB(env)
    try:
        row = db.conn.execute(
            "SELECT publication_state FROM sources WHERE id=?", ("aaa",)
        ).fetchone()
        assert row["publication_state"] == "raw"
    finally:
        db.close()
    out = capsys.readouterr().out
    assert "aaa" in out and "canonical" in out


def test_cli_blocker_exits_nonzero(env, tmp_path, capsys):
    db = GraphDB(env)
    try:
        src = _insert_note(db, id="aaa", title="A", tags=["signpost"])
        db.insert_comment(src.id, "pending")
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], [])
    finally:
        db.close()
    rc = promote.run(["--allowlist", str(allow)])
    assert rc == 2


def test_cli_applies_and_reports(env, tmp_path, capsys):
    db = GraphDB(env)
    try:
        _insert_note(db, id="aaa", title="A", tags=["signpost"])
        allow = _write_allowlist(tmp_path / "a.yaml", ["aaa"], [])
    finally:
        db.close()
    rc = promote.run(["--allowlist", str(allow)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "transitions: 1" in out
    assert "aaa" in out
    # Verify the audit note landed.
    db = GraphDB(env)
    try:
        n = db.conn.execute(
            "SELECT COUNT(*) AS n FROM sources WHERE type='note' AND publication_state='canonical'"
        ).fetchone()["n"]
        # aaa (promoted) + the audit note itself
        assert n >= 2
    finally:
        db.close()
