"""`graph sessions --all` ingests the operator's own transcripts from the
read-only home mount (auto-4j8hm, graph://89d3c8df-544 §5, driver S8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph import ingest
from tools.graph.db import GraphDB
from tools.graph.ingest import catch_up_sweep


HOST_HOME = "/home/operator"


def _claude(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "user", "uuid": "u1",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-05-01T10:00:00Z",
    }) + "\n")


def _codex(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = "2026-05-01T10:00:00Z"
    entries = [
        {"type": "session_meta", "timestamp": ts,
         "payload": {"originator": "codex-tui", "model_provider": "openai",
                     "cli_version": "0.146.0"}},
        {"type": "response_item", "timestamp": ts,
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


@pytest.fixture
def host_home_env(tmp_path, monkeypatch):
    """A node whose operator home is mounted at a temp /host-home holding two
    Claude transcripts and one Codex rollout."""
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_HOST_HOME", HOST_HOME)
    (tmp_path / "data" / "agent-runs").mkdir(parents=True)
    monkeypatch.setattr(ingest, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(ingest, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(ingest.Path, "home", lambda: tmp_path / "node-home")
    mount = tmp_path / "host-home"
    monkeypatch.setattr(ingest, "HOST_HOME_MOUNT", mount)
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    import importlib
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)

    files = [
        mount / ".claude" / "projects" / "-home-operator-code-a" / "aaaa.jsonl",
        mount / ".claude" / "projects" / "-home-operator-code-b" / "bbbb.jsonl",
        mount / ".codex" / "sessions" / "2026" / "05" / "01" / "rollout-2026-05-01T10-00-00-cccc.jsonl",
    ]
    _claude(files[0], "first claude transcript")
    _claude(files[1], "second claude transcript")
    _codex(files[2], "a codex rollout")
    return mount, files


def _sources(org: str = "personal") -> list[dict]:
    db = GraphDB(ingest.resolve_caller_db_path(org))
    try:
        rows = db.conn.execute(
            "SELECT id, file_path FROM sources WHERE type = 'session'"
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


def test_sweep_ingests_every_host_transcript_under_the_canonical_home(host_home_env):
    mount, files = host_home_env

    result = catch_up_sweep()

    assert result["scanned"] == 3
    assert [r["status"] for r in result["results"]] == ["ingested"] * 3
    paths = sorted(s["file_path"] for s in _sources())
    assert paths == sorted(
        f"{HOST_HOME}/" + str(f.relative_to(mount)) for f in files
    )


def test_second_sweep_ingests_nothing_new(host_home_env):
    catch_up_sweep()
    result = catch_up_sweep()

    assert result["unchanged"] == 3
    assert result["changed"] == 0
    assert len(_sources()) == 3


def test_a_transcript_recorded_by_the_native_path_is_matched(host_home_env):
    """A row the native host path recorded at the host-canonical path is the
    same source, not a duplicate."""
    mount, files = host_home_env
    native = files[0]
    canonical = f"{HOST_HOME}/" + str(native.relative_to(mount))
    db = GraphDB(ingest.resolve_caller_db_path("personal"))
    try:
        first = ingest.ingest_session_file(db, native)
        row = db.conn.execute(
            "SELECT file_path FROM sources WHERE id = ?", (first["source_id"],),
        ).fetchone()
    finally:
        db.close()
    assert row["file_path"] == canonical

    catch_up_sweep()

    rows = [s for s in _sources() if s["file_path"].endswith("aaaa.jsonl")]
    assert [r["id"] for r in rows] == [first["source_id"]]
    assert len(_sources()) == 3


def test_absent_mount_is_skipped_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "HOST_HOME_MOUNT", tmp_path / "absent")
    monkeypatch.setattr(ingest, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(ingest.Path, "home", lambda: tmp_path / "node-home")
    assert ingest._scan_session_files() == []
