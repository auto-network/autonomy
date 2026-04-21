"""Per-org write routing — every ``ops.*`` write lands in ``caller_org``'s DB.

Covers auto-txg5.3: activates the ``caller_org`` parameter that earlier
beads plumbed through ``ops.*``. Routing cascade (see module docstring
in ``tools.graph.ops``):

  1. Explicit ``caller_org=`` kwarg
  2. ``GRAPH_ORG`` env var
  3. Scopeless default → ``personal``

``GRAPH_DB`` env var still wins above all of the above (test pinning).

Also covers:

* ``settings_ops`` writes honour the same cascade.
* Session ingest routes per ``.session_meta.json.graph_org`` (legacy
  ``graph_project`` field accepted for pre-rename sessions), defaulting
  to ``personal.db`` for sessions with no org context.
* ``session_launcher`` bakes ``graph_org`` into session meta and exports
  ``GRAPH_ORG`` into the container env.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools.graph import db as graph_db_mod
from tools.graph import ops, settings_ops, schemas
from tools.graph.db import GraphDB, resolve_caller_db_path
from tools.graph.ingest import _open_db_for_session, session_target_org


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    """Pin ``AUTONOMY_ORGS_DIR`` + ``DEFAULT_DB`` to tmp, unset ``GRAPH_DB``.

    Every write-routing test must clear ``GRAPH_DB`` because the dispatch
    container exports it; tests inherit that env in CI otherwise.
    ``DEFAULT_DB`` redirects so the legacy fallback branch never touches
    the real ``data/graph.db``.
    """
    root = tmp_path / "orgs"
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(graph_db_mod, "DEFAULT_DB", legacy)
    return root


def _count_sources(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        conn.close()


def _count_settings(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
    finally:
        conn.close()


def _seed_note_source(db: GraphDB, *, title: str) -> str:
    from tools.graph.models import Source
    src = Source(
        type="note", platform="local", project="autonomy",
        title=title, file_path=f"note:{title.replace(' ', '_')}",
        metadata={"tags": [], "author": "test"},
    )
    db.insert_source(src)
    return src.id


# ── Write routing: explicit caller_org lands in that org's DB ──────────


def test_explicit_caller_org_routes_add_tag_to_that_db(orgs_root):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    anchore_db = GraphDB(orgs_root / "anchore.db")
    src_id = _seed_note_source(anchore_db, title="anchore note")
    anchore_db.close()

    ok = ops.add_tag(src_id, "routed", caller_org="anchore")
    assert ok is True

    # Verify the tag actually landed in anchore.db — open and read directly.
    row = sqlite3.connect(str(orgs_root / "anchore.db")).execute(
        "SELECT metadata FROM sources WHERE id = ?", (src_id,),
    ).fetchone()
    assert row is not None
    tags = json.loads(row[0]).get("tags") or []
    assert "routed" in tags


def test_explicit_caller_org_routes_add_comment_to_that_db(orgs_root):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    anchore_db = GraphDB(orgs_root / "anchore.db")
    src_id = _seed_note_source(anchore_db, title="commentable")
    anchore_db.close()

    result = ops.add_comment(src_id, "routed comment", caller_org="anchore")
    assert result["source_id"] == src_id

    # The comment should exist in anchore.db; personal.db should be empty.
    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM note_comments WHERE source_id = ?", (src_id,),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM note_comments WHERE source_id = ?", (src_id,),
        ).fetchone()[0] == 0
    finally:
        ac.close()
        pc.close()


def test_explicit_caller_org_routes_insert_capture_to_that_db(orgs_root):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    ops.insert_capture("cap-1", "routed capture", caller_org="anchore")

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-1",),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-1",),
        ).fetchone()[0] == 0
    finally:
        ac.close()
        pc.close()


def test_explicit_caller_org_routes_insert_thread_to_that_db(orgs_root):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    ops.insert_thread("thread-1", "routed thread", caller_org="anchore")

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM threads WHERE id = ?", ("thread-1",),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM threads WHERE id = ?", ("thread-1",),
        ).fetchone()[0] == 0
    finally:
        ac.close()
        pc.close()


def test_explicit_caller_org_routes_update_tag_description(orgs_root):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    ops.update_tag_description("security", "Security notes", caller_org="anchore")

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        ac_row = ac.execute(
            "SELECT description FROM tags WHERE name = ?", ("security",),
        ).fetchone()
        pc_row = pc.execute(
            "SELECT description FROM tags WHERE name = ?", ("security",),
        ).fetchone()
        assert ac_row is not None and ac_row[0] == "Security notes"
        assert pc_row is None
    finally:
        ac.close()
        pc.close()


# ── GRAPH_ORG env var fallback ─────────────────────────────────────────


def test_graph_org_env_drives_routing(orgs_root, monkeypatch):
    """With no explicit caller_org, ``GRAPH_ORG`` env picks the DB."""
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    monkeypatch.setenv("GRAPH_ORG", "anchore")

    ops.insert_capture("cap-env", "env-routed capture")

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-env",),
        ).fetchone()[0] == 1
        assert pc.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-env",),
        ).fetchone()[0] == 0
    finally:
        ac.close()
        pc.close()


def test_explicit_caller_org_beats_graph_org_env(orgs_root, monkeypatch):
    """Explicit kwarg wins over env — API handlers that know the caller
    pin the destination regardless of container env."""
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()

    monkeypatch.setenv("GRAPH_ORG", "anchore")

    ops.insert_capture("cap-explicit", "explicit wins", caller_org="autonomy")

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    au = sqlite3.connect(str(orgs_root / "autonomy.db"))
    try:
        assert ac.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-explicit",),
        ).fetchone()[0] == 0
        assert au.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-explicit",),
        ).fetchone()[0] == 1
    finally:
        ac.close()
        au.close()


# ── Scopeless convergence: personal.db catches writes with no org ─────


def test_scopeless_write_lands_in_personal_db(orgs_root):
    """With no caller_org, no GRAPH_ORG, and personal.db present, writes
    land in personal.db (absorbs auto-s45z9)."""
    GraphDB.create_org_db("personal", type_="personal").close()
    # autonomy.db also present so we can prove we don't accidentally pick
    # the legacy default.
    GraphDB.create_org_db("autonomy").close()

    ops.insert_capture("cap-personal", "scopeless default")

    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    au = sqlite3.connect(str(orgs_root / "autonomy.db"))
    try:
        assert pc.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-personal",),
        ).fetchone()[0] == 1
        assert au.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-personal",),
        ).fetchone()[0] == 0
    finally:
        pc.close()
        au.close()


def test_scopeless_write_falls_back_to_legacy_when_personal_absent(orgs_root):
    """Pre-bootstrap (personal.db not yet materialised): scopeless writes
    fall through to the legacy ``data/graph.db`` path so pre-migration
    installations keep working."""
    # No personal.db, no autonomy.db — only the legacy fallback exists.
    ops.insert_capture("cap-legacy", "legacy fallback")

    # The fixture redirected DEFAULT_DB to a tmp path.
    assert graph_db_mod.DEFAULT_DB.exists()
    legacy = sqlite3.connect(str(graph_db_mod.DEFAULT_DB))
    try:
        assert legacy.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-legacy",),
        ).fetchone()[0] == 1
    finally:
        legacy.close()


def test_graph_db_env_still_wins(orgs_root, tmp_path, monkeypatch):
    """``GRAPH_DB`` env pinning beats every other cascade step.

    Tests rely on this to pin a specific path (see ``test_ops.py``).
    """
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    monkeypatch.setenv("GRAPH_ORG", "anchore")
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("GRAPH_DB", str(pinned))

    ops.insert_capture("cap-pinned", "env override")

    pc = sqlite3.connect(str(pinned))
    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    try:
        assert pc.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-pinned",),
        ).fetchone()[0] == 1
        assert ac.execute(
            "SELECT COUNT(*) FROM captures WHERE id = ?", ("cap-pinned",),
        ).fetchone()[0] == 0
    finally:
        pc.close()
        ac.close()


# ── Cross-org isolation: writes to org A do not leak into org B ───────


def test_parallel_writes_to_different_orgs_do_not_cross(orgs_root):
    """Interleaved writes with different caller_org land in their own
    DBs — no cross-contamination even when the connection pool is cold
    / warm / mixed."""
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("autonomy").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    ops.insert_capture("cap-a-1", "anchore 1", caller_org="anchore")
    ops.insert_capture("cap-aut-1", "autonomy 1", caller_org="autonomy")
    ops.insert_capture("cap-p-1", "personal 1", caller_org="personal")
    ops.insert_capture("cap-a-2", "anchore 2", caller_org="anchore")

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    au = sqlite3.connect(str(orgs_root / "autonomy.db"))
    pc = sqlite3.connect(str(orgs_root / "personal.db"))
    try:
        a_ids = {r[0] for r in ac.execute("SELECT id FROM captures")}
        au_ids = {r[0] for r in au.execute("SELECT id FROM captures")}
        p_ids = {r[0] for r in pc.execute("SELECT id FROM captures")}
        assert a_ids == {"cap-a-1", "cap-a-2"}
        assert au_ids == {"cap-aut-1"}
        assert p_ids == {"cap-p-1"}
    finally:
        ac.close()
        au.close()
        pc.close()


# ── Settings writes route the same way ─────────────────────────────────


@pytest.fixture
def stub_schema():
    """Register a permissive throwaway schema for settings_ops writes."""
    class _StubSchema(schemas.SettingSchema):
        set_id = "test.routing"
        schema_revision = 1

    schemas.register_schema("test.routing", 1, _StubSchema)
    try:
        yield _StubSchema
    finally:
        from tools.graph.schemas.registry import unregister_schema
        unregister_schema("test.routing", 1)


def test_settings_add_setting_routes_by_caller_org(orgs_root, stub_schema):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    sid = settings_ops.add_setting(
        "test.routing", 1, "anchore-thing", {"ok": True},
        caller_org="anchore",
    )

    assert _count_settings(orgs_root / "anchore.db") == 1
    assert _count_settings(orgs_root / "personal.db") == 0

    ac = sqlite3.connect(str(orgs_root / "anchore.db"))
    try:
        row = ac.execute(
            "SELECT set_id, key FROM settings WHERE id = ?", (sid,),
        ).fetchone()
        assert row is not None
        assert row[0] == "test.routing"
        assert row[1] == "anchore-thing"
    finally:
        ac.close()


def test_settings_scopeless_add_setting_lands_in_personal(orgs_root, stub_schema):
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db("autonomy").close()

    settings_ops.add_setting(
        "test.routing", 1, "scopeless", {"ok": True},
    )

    assert _count_settings(orgs_root / "personal.db") == 1
    assert _count_settings(orgs_root / "autonomy.db") == 0


def test_settings_graph_org_env_drives_routing(orgs_root, stub_schema, monkeypatch):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    monkeypatch.setenv("GRAPH_ORG", "anchore")

    settings_ops.add_setting(
        "test.routing", 1, "env-driven", {"ok": True},
    )

    assert _count_settings(orgs_root / "anchore.db") == 1
    assert _count_settings(orgs_root / "personal.db") == 0


# ── Session ingest routing via .session_meta.json ──────────────────────


def _write_session_meta(dir_path: Path, meta: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    f = dir_path / ".session_meta.json"
    f.write_text(json.dumps(meta))
    return f


def test_session_target_org_prefers_graph_org(tmp_path):
    sessions = tmp_path / "sessions"
    _write_session_meta(sessions, {
        "graph_org": "anchore",
        "graph_project": "autonomy",  # legacy, should lose to graph_org
    })
    jsonl = sessions / "abc.jsonl"
    jsonl.touch()

    assert session_target_org(jsonl) == "anchore"


def test_session_target_org_falls_back_to_graph_project(tmp_path):
    sessions = tmp_path / "sessions"
    _write_session_meta(sessions, {"graph_project": "anchore"})
    jsonl = sessions / "abc.jsonl"
    jsonl.touch()

    assert session_target_org(jsonl) == "anchore"


def test_session_target_org_defaults_to_personal(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    jsonl = sessions / "no-meta.jsonl"
    jsonl.touch()

    assert session_target_org(jsonl) == "personal"


def test_open_db_for_session_routes_to_graph_org_db(orgs_root, tmp_path):
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("personal", type_="personal").close()

    sessions = tmp_path / "sessions"
    _write_session_meta(sessions, {"graph_org": "anchore"})
    jsonl = sessions / "s.jsonl"
    jsonl.touch()

    db = _open_db_for_session(jsonl)
    try:
        assert Path(db.db_path) == orgs_root / "anchore.db"
    finally:
        db.close()


def test_open_db_for_session_defaults_to_personal(orgs_root, tmp_path):
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db("autonomy").close()

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    jsonl = sessions / "s.jsonl"
    jsonl.touch()

    db = _open_db_for_session(jsonl)
    try:
        assert Path(db.db_path) == orgs_root / "personal.db"
    finally:
        db.close()


def test_open_db_for_session_graph_db_env_still_wins(orgs_root, tmp_path, monkeypatch):
    """``GRAPH_DB`` env pins the target even for per-session routing — so
    tests that pin a single DB keep working as written."""
    GraphDB.create_org_db("anchore").close()
    pinned = tmp_path / "pinned.db"
    monkeypatch.setenv("GRAPH_DB", str(pinned))

    sessions = tmp_path / "sessions"
    _write_session_meta(sessions, {"graph_org": "anchore"})
    jsonl = sessions / "s.jsonl"
    jsonl.touch()

    db = _open_db_for_session(jsonl)
    try:
        assert Path(db.db_path) == pinned
    finally:
        db.close()


# ── session_launcher emits graph_org in session meta + GRAPH_ORG env ──


def test_session_launcher_writes_graph_org_in_meta(tmp_path, monkeypatch):
    """``launch_session()`` bakes ``graph_org`` into ``.session_meta.json``
    (derived from ``graph_project`` when the caller did not supply it).

    We stub ``_resolve_credentials`` and ``subprocess.run`` so the test
    doesn't need docker; we just inspect the meta file the launcher wrote.
    """
    import agents.session_launcher as launcher

    # Force the launcher to write into tmp_path so we can read the meta.
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)

    # Stub credentials and docker run — we only care about the meta file.
    monkeypatch.setattr(launcher, "_resolve_credentials",
                        lambda: {"type": "token", "token": "x"})
    monkeypatch.setattr(launcher, "_setup_auth_docker_args",
                        lambda creds, run_dir: [])

    import subprocess

    def _fake_run(*a, **kw):
        class R:
            returncode = 0
            stdout = "CONTAINER_ID=test\n"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # detach=True still invokes subprocess.run → we stubbed it.
    launcher.launch_session(
        session_type="dispatch",
        name="test-session",
        prompt=None,
        metadata={"graph_project": "anchore", "graph_tags": ["x"]},
        detach=True,
    )

    meta_path = tmp_path / "data" / "agent-runs"
    # Find any subdir and read .session_meta.json
    candidates = list(meta_path.glob("test-session*/sessions/.session_meta.json"))
    assert candidates, f"no session meta written under {meta_path}"
    meta = json.loads(candidates[0].read_text())
    assert meta.get("graph_project") == "anchore"
    # graph_org should be derived from graph_project when absent.
    assert meta.get("graph_org") == "anchore"


def test_session_launcher_preserves_explicit_graph_org(tmp_path, monkeypatch):
    """When the caller passes ``graph_org`` in metadata, the launcher
    does not overwrite it with ``graph_project``."""
    import agents.session_launcher as launcher

    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_resolve_credentials",
                        lambda: {"type": "token", "token": "x"})
    monkeypatch.setattr(launcher, "_setup_auth_docker_args",
                        lambda creds, run_dir: [])

    import subprocess

    def _fake_run(*a, **kw):
        class R:
            returncode = 0
            stdout = "CONTAINER_ID=test\n"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    launcher.launch_session(
        session_type="dispatch",
        name="explicit-session",
        prompt=None,
        metadata={"graph_project": "autonomy", "graph_org": "anchore"},
        detach=True,
    )

    meta_path = tmp_path / "data" / "agent-runs"
    candidates = list(meta_path.glob("explicit-session*/sessions/.session_meta.json"))
    assert candidates
    meta = json.loads(candidates[0].read_text())
    assert meta.get("graph_project") == "autonomy"
    assert meta.get("graph_org") == "anchore"


def test_session_launcher_exports_graph_org_env(tmp_path, monkeypatch):
    """Container command line must include ``-e GRAPH_ORG=<slug>`` so
    the in-container ``graph`` CLI + ``ops.*`` routes to the right DB."""
    import agents.session_launcher as launcher

    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_resolve_credentials",
                        lambda: {"type": "token", "token": "x"})
    monkeypatch.setattr(launcher, "_setup_auth_docker_args",
                        lambda creds, run_dir: [])

    captured: dict = {}

    def _capture_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = "CONTAINER_ID=env-test\n"
            stderr = ""
        return R()

    import subprocess
    monkeypatch.setattr(subprocess, "run", _capture_run)

    launcher.launch_session(
        session_type="dispatch",
        name="env-session",
        prompt=None,
        metadata={"graph_project": "anchore"},
        detach=True,
    )

    # The docker cmd is a flat list of strings — look for ``-e GRAPH_ORG=...``.
    cmd = captured["cmd"]
    idx = None
    for i, arg in enumerate(cmd):
        if isinstance(arg, str) and arg == "-e" and i + 1 < len(cmd):
            nxt = cmd[i + 1]
            if isinstance(nxt, str) and nxt.startswith("GRAPH_ORG="):
                idx = i
                break
    assert idx is not None, f"GRAPH_ORG env not exported; cmd: {cmd}"
    assert cmd[idx + 1] == "GRAPH_ORG=anchore"
