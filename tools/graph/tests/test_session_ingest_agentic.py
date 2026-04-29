"""Tests for agentic-session ingest routing (auto-gh2iv).

The dashboard's ``api_agent_action_dispatch`` eager-creates an
``agentic`` source row at agent-action dispatch time. When the agent's
JSONL is later ingested, ``_ingest_text_session`` must:

  * Recognise ``meta.type == 'agentic'`` and look the source up by
    ``meta.agentic_source_id`` instead of by file_path.
  * APPEND turns to that existing row — not create a new ``session``
    row alongside it.
  * Preserve the dashboard-set title (``_derive_session_title`` runs
    only on the legacy create path).

A regression test for the non-agentic path keeps the existing
behaviour pinned: a fresh JSONL with no ``.session_meta.json`` still
creates a ``type='session'`` row by file_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.ingest import ingest_claude_code_session
from tools.graph.models import Source


# ── JSONL helpers ──────────────────────────────────────────────────────


def _user_entry(text: str, ts: str = "2026-04-28T22:00:00Z") -> dict:
    return {
        "type": "user",
        "uuid": f"u-{abs(hash(text)) & 0xffff:x}",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
    }


def _assistant_entry(text: str, ts: str = "2026-04-28T22:00:01Z") -> dict:
    return {
        "type": "assistant",
        "uuid": f"a-{abs(hash(text)) & 0xffff:x}",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-test",
            "usage": {"input_tokens": 8, "output_tokens": 12},
        },
        "timestamp": ts,
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _write_session_meta(sessions_dir: Path, doc: dict) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / ".session_meta.json").write_text(json.dumps(doc, indent=2))


@pytest.fixture
def graph_db(tmp_path) -> GraphDB:
    db = GraphDB(tmp_path / "graph.db")
    yield db
    db.close()


@pytest.fixture
def agentic_run(tmp_path):
    """Build the directory layout the dashboard's session_launcher writes.

    layout::
        <run>/sessions/.session_meta.json
        <run>/sessions/-workspace-repo/<uuid>.jsonl
    """
    run_dir = tmp_path / "data" / "agent-runs" / "agentic-update-summary-test"
    sessions_dir = run_dir / "sessions"
    project_dir = sessions_dir / "-workspace-repo"
    jsonl_path = project_dir / "61d488cc-test-session-uuid.jsonl"
    return run_dir, sessions_dir, project_dir, jsonl_path


# ══════════════════════════════════════════════════════════════════════
# TestAgenticIngestRouting — agentic JSONL appends to existing source
# ══════════════════════════════════════════════════════════════════════


class TestAgenticIngestRouting:
    def _seed_agentic_source(self, graph_db: GraphDB, src_id: str) -> str:
        """Mimic insert_agentic_session(): create a type='agentic' source row.

        Returns the source id so tests can verify ingest matched it.
        """
        source = Source(
            id=src_id,
            type="agentic",
            platform="local",
            project="autonomy",
            title="Update Title & Summary",  # set by dashboard endpoint
            file_path="agentic:agentic-update-summary-test",
            metadata={
                "kind": "agent-action",
                "set_id": "dashboard.agent-actions",
                "member_key": "note.update-summary",
                "session_type": "agentic",
                "slug": "agentic-update-summary-test",
            },
            publication_state="raw",
        )
        graph_db.insert_source(source)
        graph_db.commit()
        return source.id

    def test_appends_to_existing_source_no_new_row(self, graph_db, agentic_run):
        """meta.type=agentic + agentic_source_id → append; do NOT INSERT."""
        run_dir, sessions_dir, project_dir, jsonl_path = agentic_run
        src_id = "bd2a78f2-da73-453c-b873-9002ae33c4bf"
        self._seed_agentic_source(graph_db, src_id)

        starting = graph_db.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE type = 'agentic'"
        ).fetchone()[0]
        assert starting == 1, "fixture should leave exactly one agentic row"

        _write_session_meta(sessions_dir, {
            "type": "agentic",
            "container_name": "agentic-update-summary-test",
            "agentic_source_id": src_id,
            "set_id": "dashboard.agent-actions",
            "member_key": "note.update-summary",
            "graph_org": "autonomy",
            "graph_project": "autonomy",
        })

        _write_jsonl(jsonl_path, [
            _user_entry("Update title and summary please."),
            _assistant_entry("Reading the note now."),
            _assistant_entry("Title set; short_description set."),
        ])

        result = ingest_claude_code_session(graph_db, jsonl_path, force=True)

        # Status should be the agentic-specific marker so callers (and
        # logs) can distinguish the new branch from the legacy path.
        assert result["status"] in ("agentic_updated", "agentic_refreshed"), result
        assert result["source_id"] == src_id

        # No new agentic source row should have been created.
        ending = graph_db.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE type = 'agentic'"
        ).fetchone()[0]
        assert ending == starting, (
            "agentic ingest must NOT create a new source row — must append "
            "onto the existing dashboard-eager-created row"
        )

        # Turns must land on the existing agentic source.
        n_thoughts = graph_db.conn.execute(
            "SELECT COUNT(*) FROM thoughts WHERE source_id = ?", (src_id,),
        ).fetchone()[0]
        n_derivations = graph_db.conn.execute(
            "SELECT COUNT(*) FROM derivations WHERE source_id = ?", (src_id,),
        ).fetchone()[0]
        assert n_thoughts >= 1
        assert n_derivations >= 1

    def test_preserves_dashboard_title(self, graph_db, agentic_run):
        """Title set by the dashboard endpoint must NOT be overwritten.

        ``_derive_session_title`` runs in the legacy create path; the
        agentic branch passes ``title=None`` to ``update_source_summary``
        so the dashboard-supplied label stays intact.
        """
        run_dir, sessions_dir, project_dir, jsonl_path = agentic_run
        src_id = "44444444-4444-4444-4444-444444444444"
        self._seed_agentic_source(graph_db, src_id)

        original = graph_db.get_source(src_id)
        assert original["title"] == "Update Title & Summary"

        _write_session_meta(sessions_dir, {
            "type": "agentic",
            "agentic_source_id": src_id,
            "graph_org": "autonomy",
        })
        _write_jsonl(jsonl_path, [
            _user_entry("This is a different first turn that would otherwise "
                        "become a derived title from the legacy ingest path."),
            _assistant_entry("Acknowledged."),
        ])

        ingest_claude_code_session(graph_db, jsonl_path, force=True)

        refreshed = graph_db.get_source(src_id)
        assert refreshed["title"] == "Update Title & Summary", (
            "agentic ingest must preserve the dashboard-set title — "
            "not overwrite from the first-turn content"
        )


class TestNonAgenticIngestUnchanged:
    """Regression: a session JSONL without agentic meta still creates a
    ``type='session'`` row by file_path. This pins the legacy path so the
    new agentic branch doesn't accidentally reroute claude-code, codex,
    dispatch, or librarian sessions."""

    def test_legacy_session_creates_new_row(self, graph_db, tmp_path):
        sessions_dir = tmp_path / "sessions"
        jsonl_path = sessions_dir / "12345678-legacy-session.jsonl"

        # Required by _open_db_for_session: need a graph_org in meta so
        # the file isn't skipped, but the type stays plain 'session'.
        _write_session_meta(sessions_dir, {
            "graph_org": "autonomy",
            "graph_project": "autonomy",
        })

        _write_jsonl(jsonl_path, [
            _user_entry("Legacy session first turn"),
            _assistant_entry("Legacy session reply"),
        ])

        starting_session = graph_db.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE type = 'session'"
        ).fetchone()[0]

        result = ingest_claude_code_session(graph_db, jsonl_path, force=True)
        assert result["status"] == "ingested"

        ending_session = graph_db.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE type = 'session'"
        ).fetchone()[0]
        assert ending_session == starting_session + 1, (
            "Non-agentic JSONL must still produce a fresh type='session' row"
        )
