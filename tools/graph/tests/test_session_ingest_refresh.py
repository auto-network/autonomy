"""Tests for session-source title derivation, summary refresh, and last_activity_at.

Covers auto-elz1: Recent Sessions fidelity. Three downstream behaviors that
must hold so the dashboard's Recent Sessions list stops being stale.

  1. Title derivation prefers (in order): dashboard.db label, bead-id+title,
     tmux/container name, then a content turn — skipping `[Image #N]` and
     `[dashboard] confirming terminal link` placeholders.

  2. Incremental ingest refreshes `total_turns`, `ended_at`, `last_activity_at`,
     `file_size`, and the title — even if no new content turns landed (the
     file may have grown via tool_use/tool_result entries).

  3. Pure no-op (file size unchanged) still short-circuits without parsing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.graph.db import GraphDB
from tools.graph.ingest import (
    _derive_session_title,
    _is_low_signal_title,
    ingest_claude_code_session,
)


# ── JSONL helpers ──────────────────────────────────────────────────────


def _user_entry(text: str, ts: str = "2026-04-19T12:00:00Z") -> dict:
    return {
        "type": "user",
        "uuid": f"u-{hash(text) & 0xffff:x}",
        "message": {"role": "user", "content": text},
        "timestamp": ts,
    }


def _assistant_entry(text: str, ts: str = "2026-04-19T12:00:01Z") -> dict:
    return {
        "type": "assistant",
        "uuid": f"a-{hash(text) & 0xffff:x}",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-test",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        "timestamp": ts,
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def graph_db(tmp_path) -> GraphDB:
    """A fresh on-disk GraphDB for each test (so the migration runs end-to-end)."""
    db = GraphDB(tmp_path / "graph.db")
    yield db
    db.close()


@pytest.fixture
def jsonl_path(tmp_path) -> Path:
    """Path with a JSONL-shaped name; populated per-test."""
    return tmp_path / "sessions" / "fa5a5a5a-test-uuid-0001.jsonl"


# ══════════════════════════════════════════════════════════════════════
# TestSchemaMigration — last_activity_at column appears
# ══════════════════════════════════════════════════════════════════════


class TestSchemaMigration:
    def test_last_activity_at_column_exists(self, graph_db):
        cols = {r[1] for r in graph_db.conn.execute("PRAGMA table_info(sources)").fetchall()}
        assert "last_activity_at" in cols

    def test_backfill_uses_metadata_ended_at(self, tmp_path):
        """Migration on a pre-existing DB backfills last_activity_at from metadata."""
        db_path = tmp_path / "legacy.db"
        # Create the legacy schema (no last_activity_at)
        legacy = sqlite3.connect(str(db_path))
        legacy.execute("""CREATE TABLE sources (
            id TEXT PRIMARY KEY, type TEXT, platform TEXT, project TEXT,
            title TEXT, url TEXT, file_path TEXT UNIQUE,
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT '',
            ingested_at TEXT NOT NULL DEFAULT ''
        )""")
        legacy.execute(
            "INSERT INTO sources (id, type, file_path, metadata, created_at, ingested_at)"
            " VALUES (?, 'session', ?, ?, ?, ?)",
            ("legacy-1", "/tmp/legacy.jsonl",
             json.dumps({"ended_at": "2026-04-15T10:00:00Z"}),
             "2026-04-10T00:00:00Z", "2026-04-15T10:01:00Z"),
        )
        legacy.commit()
        legacy.close()

        # Open via GraphDB — migration runs
        db = GraphDB(db_path)
        row = db.conn.execute(
            "SELECT last_activity_at FROM sources WHERE id = ?", ("legacy-1",)
        ).fetchone()
        assert row["last_activity_at"] == "2026-04-15T10:00:00Z"
        db.close()


# ══════════════════════════════════════════════════════════════════════
# TestLowSignalTitle — placeholder content gets rejected
# ══════════════════════════════════════════════════════════════════════


class TestLowSignalTitle:
    def test_image_placeholder_rejected(self):
        assert _is_low_signal_title("[Image #1]")
        assert _is_low_signal_title("[Image #42]")

    def test_handshake_text_rejected(self):
        assert _is_low_signal_title(
            "[dashboard] confirming terminal link — please reply with I SEE IT"
        )

    def test_real_content_accepted(self):
        assert not _is_low_signal_title("Can you help me with the session cards?")

    def test_empty_rejected(self):
        assert _is_low_signal_title("")
        assert _is_low_signal_title(None)


# ══════════════════════════════════════════════════════════════════════
# TestTitleDerivation — preference order
# ══════════════════════════════════════════════════════════════════════


class TestTitleDerivation:
    """Title preference: label > bead > container_name > content fallback."""

    def test_dashboard_label_wins(self, jsonl_path):
        """When dashboard.db has a user-set label, it overrides everything else."""
        turns = [{"role": "user", "content": "What do you see?", "turn_number": 1}]
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value="My nice label"):
            with patch("tools.graph.ingest._lookup_bead_title", return_value="Bead title"):
                title = _derive_session_title({}, jsonl_path,
                                              {"bead_id": "auto-test", "container_name": "tmux-x"},
                                              turns)
        assert title == "My nice label"

    def test_bead_title_when_no_label(self, jsonl_path):
        """Without a label, dispatch sessions get bead_id + bead title."""
        turns = [{"role": "user", "content": "What do you see?", "turn_number": 1}]
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            with patch("tools.graph.ingest._lookup_bead_title", return_value="Fix the thing"):
                title = _derive_session_title({}, jsonl_path,
                                              {"bead_id": "auto-test"}, turns)
        assert title == "auto-test: Fix the thing"

    def test_bead_id_alone_when_dolt_unreachable(self, jsonl_path):
        """If the beads DB is offline, fall back to the bead_id alone."""
        turns = [{"role": "user", "content": "doing work", "turn_number": 1}]
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            with patch("tools.graph.ingest._lookup_bead_title", return_value=None):
                title = _derive_session_title({}, jsonl_path,
                                              {"bead_id": "auto-test"}, turns)
        assert title == "auto-test"

    def test_container_name_when_no_bead(self, jsonl_path):
        """Interactive sessions without label/bead use the container/tmux name."""
        turns = [{"role": "user", "content": "[Image #1]", "turn_number": 1}]
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            title = _derive_session_title({}, jsonl_path,
                                          {"container_name": "auto-0418-174403"}, turns)
        assert title == "auto-0418-174403"

    def test_skips_image_placeholder_first_turn(self, jsonl_path):
        """First turn = [Image #1] should be skipped in favor of next text turn."""
        turns = [
            {"role": "user", "content": "[Image #1]", "turn_number": 1},
            {"role": "assistant", "content": "I see your image", "turn_number": 2},
            {"role": "user", "content": "What do you think?", "turn_number": 3},
        ]
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            title = _derive_session_title({}, jsonl_path, {}, turns)
        assert title == "What do you think?"

    def test_skips_handshake_first_turn(self, jsonl_path):
        """Handshake text is filtered — fall back to next user turn."""
        turns = [
            {"role": "user",
             "content": "[dashboard] confirming terminal link — please reply with I SEE IT",
             "turn_number": 1},
            {"role": "user", "content": "I SEE IT", "turn_number": 2},
            {"role": "user", "content": "Now build the thing", "turn_number": 3},
        ]
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            title = _derive_session_title({}, jsonl_path, {}, turns)
        # "I SEE IT" is short but not flagged as low-signal; it's the next non-handshake user turn
        assert title in ("I SEE IT", "Now build the thing")

    def test_first_text_turn_when_no_metadata(self, jsonl_path):
        """No label, no bead, no container → use first user content turn."""
        turns = [{"role": "user", "content": "Help me debug this", "turn_number": 1}]
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            title = _derive_session_title({}, jsonl_path, {}, turns)
        assert title == "Help me debug this"


# ══════════════════════════════════════════════════════════════════════
# TestIncrementalRefresh — summary fields update on every pass
# ══════════════════════════════════════════════════════════════════════


class TestIncrementalRefresh:
    def test_first_ingest_writes_last_activity_at(self, graph_db, jsonl_path):
        """Fresh ingest populates last_activity_at from the latest turn timestamp."""
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(jsonl_path, [
            _user_entry("Hello there", ts="2026-04-19T10:00:00Z"),
            _assistant_entry("Hello back to you", ts="2026-04-19T10:00:05Z"),
        ])
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            result = ingest_claude_code_session(graph_db, jsonl_path)

        assert result["status"] == "ingested"
        row = graph_db.conn.execute(
            "SELECT last_activity_at, metadata FROM sources WHERE id = ?",
            (result["source_id"],),
        ).fetchone()
        assert row["last_activity_at"] == "2026-04-19T10:00:05Z"
        meta = json.loads(row["metadata"])
        assert meta["total_turns"] == 2
        assert meta["ended_at"] == "2026-04-19T10:00:05Z"

    def test_incremental_refresh_updates_total_turns(self, graph_db, jsonl_path):
        """Re-ingesting a grown JSONL refreshes total_turns + ended_at."""
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(jsonl_path, [
            _user_entry("Hello", ts="2026-04-19T10:00:00Z"),
            _assistant_entry("Hello back to you", ts="2026-04-19T10:00:05Z"),
        ])
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            r1 = ingest_claude_code_session(graph_db, jsonl_path)
            source_id = r1["source_id"]

            # Append more turns
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(_user_entry("More questions please", ts="2026-04-19T11:00:00Z")) + "\n")
                f.write(json.dumps(_assistant_entry("Of course here's a reply", ts="2026-04-19T11:00:05Z")) + "\n")

            r2 = ingest_claude_code_session(graph_db, jsonl_path)

        assert r2["status"] == "updated"
        assert r2["source_id"] == source_id

        row = graph_db.conn.execute(
            "SELECT last_activity_at, metadata FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        assert row["last_activity_at"] == "2026-04-19T11:00:05Z"
        meta = json.loads(row["metadata"])
        assert meta["total_turns"] == 4
        assert meta["ended_at"] == "2026-04-19T11:00:05Z"

    def test_no_op_when_file_unchanged(self, graph_db, jsonl_path):
        """File size unchanged → fast path skip; no parse."""
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(jsonl_path, [
            _user_entry("Hello", ts="2026-04-19T10:00:00Z"),
            _assistant_entry("Hello back to you", ts="2026-04-19T10:00:05Z"),
        ])
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            ingest_claude_code_session(graph_db, jsonl_path)
            r2 = ingest_claude_code_session(graph_db, jsonl_path)
        assert r2["status"] == "skipped"
        assert r2["reason"] == "already up to date"

    def test_title_refresh_when_label_appears_after_first_ingest(self, graph_db, jsonl_path):
        """If the user runs `set-label` after first ingest, the next pass adopts it."""
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(jsonl_path, [
            _user_entry("[Image #1]", ts="2026-04-19T10:00:00Z"),
        ])
        # First ingest with no label → falls back to content (which is low-signal)
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            r1 = ingest_claude_code_session(graph_db, jsonl_path)
        first_title = graph_db.conn.execute(
            "SELECT title FROM sources WHERE id = ?", (r1["source_id"],)
        ).fetchone()["title"]
        # With only an [Image #1] turn and no metadata, title should be None or empty
        assert not first_title

        # Append turns + simulate set-label firing on the dashboard
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_user_entry("real work", ts="2026-04-19T11:00:00Z")) + "\n")

        with patch("tools.graph.ingest._lookup_dashboard_label",
                   return_value="Session viewer redesign"):
            r2 = ingest_claude_code_session(graph_db, jsonl_path)

        assert r2["status"] == "updated"
        new_title = graph_db.conn.execute(
            "SELECT title FROM sources WHERE id = ?", (r1["source_id"],)
        ).fetchone()["title"]
        assert new_title == "Session viewer redesign"


# ══════════════════════════════════════════════════════════════════════
# TestCompactSummaryIngest — context-compaction boundary turns
# ══════════════════════════════════════════════════════════════════════


def _compact_summary_entry(body: str, ts: str = "2026-04-19T12:30:00Z") -> dict:
    return {
        "type": "user",
        "uuid": "cs-uuid-1",
        "isCompactSummary": True,
        "isVisibleInTranscriptOnly": True,
        "message": {"role": "user", "content": body},
        "timestamp": ts,
    }


def _compact_meta_system_entry(ts: str = "2026-04-19T12:29:59Z") -> dict:
    return {
        "type": "system",
        "uuid": "cs-meta-1",
        "compactMetadata": {"turnsCompacted": 240, "preCompactionTokens": 198000},
        "timestamp": ts,
    }


SUMMARY_BODY = (
    "This session is being continued from a previous conversation that ran out "
    "of context. The prior session: built a dashboard, wired the store, "
    "deployed it to staging. Pending: add tests, write docs."
)


class TestCompactSummaryIngest:
    """Compact-summary turns get role='compact_summary', not role='user'."""

    def test_compact_summary_ingested_with_distinct_role(self, graph_db, jsonl_path):
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(jsonl_path, [
            _user_entry("First real question", ts="2026-04-19T10:00:00Z"),
            _assistant_entry("Sure, here's an answer", ts="2026-04-19T10:00:05Z"),
            _compact_meta_system_entry(ts="2026-04-19T12:29:59Z"),
            _compact_summary_entry(SUMMARY_BODY, ts="2026-04-19T12:30:00Z"),
            _user_entry("Keep going please", ts="2026-04-19T12:31:00Z"),
        ])
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            result = ingest_claude_code_session(graph_db, jsonl_path)

        assert result["status"] == "ingested"
        source_id = result["source_id"]

        rows = graph_db.conn.execute(
            "SELECT content, role, turn_number, metadata FROM thoughts "
            "WHERE source_id = ? ORDER BY turn_number",
            (source_id,),
        ).fetchall()

        roles_by_content = {r["content"][:30]: r["role"] for r in rows}
        assert roles_by_content["First real question"] == "user"
        assert roles_by_content["Keep going please"] == "user"
        # Compact summary is indexed but tagged compact_summary
        cs_rows = [r for r in rows if r["role"] == "compact_summary"]
        assert len(cs_rows) == 1
        assert cs_rows[0]["content"] == SUMMARY_BODY
        # compactMetadata folded into the summary thought's metadata
        meta = json.loads(cs_rows[0]["metadata"])
        assert meta.get("compact_metadata", {}).get("turnsCompacted") == 240

    def test_compact_summary_skipped_by_title_probe(self, graph_db, jsonl_path):
        """Session title derivation must not pick up the compact-summary body."""
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(jsonl_path, [
            _compact_summary_entry(SUMMARY_BODY, ts="2026-04-19T10:00:00Z"),
            _user_entry("My real first question", ts="2026-04-19T10:00:05Z"),
        ])
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            result = ingest_claude_code_session(graph_db, jsonl_path)

        title = graph_db.conn.execute(
            "SELECT title FROM sources WHERE id = ?", (result["source_id"],)
        ).fetchone()["title"]
        assert title == "My real first question"

    def test_compact_summary_findable_via_fts(self, graph_db, jsonl_path):
        """FTS still surfaces the summary content after role re-tag."""
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(jsonl_path, [
            _compact_summary_entry(SUMMARY_BODY, ts="2026-04-19T10:00:00Z"),
        ])
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            ingest_claude_code_session(graph_db, jsonl_path)

        rows = graph_db.conn.execute(
            "SELECT t.role FROM thoughts_fts fts "
            "JOIN thoughts t ON t.rowid = fts.rowid "
            "WHERE thoughts_fts MATCH ?",
            ("dashboard",),
        ).fetchall()
        assert any(r["role"] == "compact_summary" for r in rows)

    def test_compact_summary_visible_in_transcript_only_alt_flag(self, graph_db, jsonl_path):
        """isVisibleInTranscriptOnly alone (no isCompactSummary) still triggers."""
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "type": "user",
            "uuid": "vto-1",
            "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content": SUMMARY_BODY},
            "timestamp": "2026-04-19T10:00:00Z",
        }
        _write_jsonl(jsonl_path, [
            _user_entry("intro question", ts="2026-04-19T09:59:00Z"),
            raw,
        ])
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            result = ingest_claude_code_session(graph_db, jsonl_path)

        cs_rows = graph_db.conn.execute(
            "SELECT role FROM thoughts WHERE source_id = ? AND role = 'compact_summary'",
            (result["source_id"],),
        ).fetchall()
        assert len(cs_rows) == 1
