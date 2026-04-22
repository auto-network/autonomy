"""Tests for server-side semantic tile enrichment (Boundary C — parsed entry + graph.db → enriched entry).

Tests `_enrich_semantic_tile()` as a unit function that enriches semantic_bash entries
(note-created, thought-captured, comment-added) with title, preview, and tags from graph.db.

Expected test status on CURRENT code (before auto-h4gh):
- TestEnrichmentPopulatesFields: FAIL — `_enrich_semantic_tile()` doesn't exist yet.
- TestEnrichmentFallback: PASS — entries without enrichment render with raw content (current behavior).
- TestEnrichmentCodePath: FAIL — enrichment function doesn't exist yet.

Architecture Spec v7 Section 3d:
    When a tool_result is classified as note-created, thought-captured, or comment-added,
    the parser enriches it inline with one SQLite read from graph.db:
    1. Look up source by source_id (or comment_id → parent source)
    2. Extract title (strip leading # headings)
    3. Extract tags from source metadata
    4. Extract preview (first 120 chars of content, skip heading lines)
    5. Add title, preview, tags to the entry dict
"""

import json
import sqlite3

import pytest

from tools.dashboard.session_harness import parse_claude_log_line as _parse_jsonl_entry


# ── Test graph.db fixture ─────────────────────────────────────────────

@pytest.fixture
def test_graph_db(tmp_path):
    """Minimal graph.db with known sources for enrichment testing."""
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE sources (
        id TEXT PRIMARY KEY, title TEXT, metadata TEXT, created_at TEXT
    )""")
    conn.execute("""CREATE TABLE thoughts (
        id TEXT, source_id TEXT, content TEXT, turn_number INTEGER
    )""")
    # Known note source (IDs must be hex-like to match [a-f0-9-] regex in parser)
    conn.execute("INSERT INTO sources VALUES (?, ?, ?, ?)", (
        "a1b2c3d4-0001",
        "# Session Viewer Redesign",
        '{"tags": ["architecture", "session-viewer"]}',
        "2026-03-25",
    ))
    conn.execute("INSERT INTO thoughts VALUES (?, ?, ?, ?)", (
        "a001", "a1b2c3d4-0001",
        "# Session Viewer Redesign\n\nThis note describes the architecture for the unified session viewer.",
        1,
    ))
    # Known thought source
    conn.execute("INSERT INTO sources VALUES (?, ?, ?, ?)", (
        "b2c3d4e5-0002",
        "auth needs passkeys",
        '{"tags": ["auth", "thought"]}',
        "2026-03-25",
    ))
    conn.execute("INSERT INTO thoughts VALUES (?, ?, ?, ?)", (
        "b002", "b2c3d4e5-0002",
        "auth needs passkeys — the current session token approach is too fragile",
        1,
    ))
    # Known comment parent — a note that has received a comment
    conn.execute("INSERT INTO sources VALUES (?, ?, ?, ?)", (
        "c3d4e5f6-0003",
        "# Deployment Checklist",
        '{"tags": ["ops", "deployment"]}',
        "2026-03-25",
    ))
    conn.execute("INSERT INTO thoughts VALUES (?, ?, ?, ?)", (
        "c003", "c3d4e5f6-0003",
        "# Deployment Checklist\n\n1. Run tests\n2. Check migrations\n3. Deploy to staging",
        1,
    ))
    # Comment source — references the parent note
    conn.execute("INSERT INTO sources VALUES (?, ?, ?, ?)", (
        "d4e5f6a7-0004",
        "Comment on Deployment Checklist",
        json.dumps({"tags": ["ops"], "parent_source_id": "c3d4e5f6-0003"}),
        "2026-03-25",
    ))

    conn.commit()
    conn.close()
    return str(db_path)


# ── JSONL line helpers ────────────────────────────────────────────────

TS = "2026-03-25T14:00:00Z"


def _line(obj: dict) -> str:
    """Serialize a dict to a JSONL line string."""
    return json.dumps(obj)


def _semantic(result):
    """Extract the semantic_bash entry when the parser upconverts.

    `_parse_jsonl_entry` augments (not replaces) when upconversion fires —
    it returns a list `[tool_result, semantic_bash]`. Tests that reach for
    `result["type"] == "semantic_bash"` need the semantic half, so pick it.
    Leaves non-list results (no upconversion path) untouched.
    """
    if isinstance(result, list):
        for e in result:
            if isinstance(e, dict) and e.get("type") == "semantic_bash":
                return e
        return result[0] if result else None
    return result


def _make_tool_result_note_saved(source_id: str) -> dict:
    """JSONL entry for a tool_result containing 'Note saved (src:...)'."""
    return {
        "type": "tool_result", "toolUseId": "toolu_note",
        "message": {"role": "user", "content": f"\u2713 Note saved (src:{source_id})\nTags: architecture, session-viewer"},
        "timestamp": TS,
    }


def _make_tool_result_thought_captured(source_id: str) -> dict:
    """JSONL entry for a tool_result containing '✓ Captured: ...'."""
    return {
        "type": "tool_result", "toolUseId": "toolu_thought",
        "message": {"role": "user", "content": f"\u2713 Captured: {source_id} (thought about auth passkeys)"},
        "timestamp": TS,
    }


def _make_tool_result_comment_added(comment_id: str, parent_source_id: str) -> dict:
    """JSONL entry for a tool_result containing 'Comment added (id:...)'."""
    return {
        "type": "tool_result", "toolUseId": "toolu_comment",
        "message": {"role": "user", "content": f"\u2713 Comment added (id:{comment_id}) on {parent_source_id}"},
        "timestamp": TS,
    }


def _make_regular_tool_result() -> dict:
    """JSONL entry for a regular tool_result (not a semantic tile)."""
    return {
        "type": "tool_result", "toolUseId": "toolu_read",
        "message": {"role": "user", "content": "file1.py\nfile2.py\nfile3.py"},
        "timestamp": TS,
    }


def _make_user_list_with_note_saved(source_id: str) -> dict:
    """JSONL entry where note-created appears inside a user message content list."""
    return {
        "parentUuid": "uuu", "isSidechain": False, "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_note_inner",
             "content": f"\u2713 Note saved (src:{source_id})\nTags: architecture"},
        ]},
        "timestamp": TS,
    }


# ══════════════════════════════════════════════════════════════════════
# TestEnrichmentPopulatesFields — server-side enrichment adds metadata
#
# Expected: FAIL on current code (_enrich_semantic_tile doesn't exist).
# auto-h4gh adds _enrich_semantic_tile() to server.py, called from
# _parse_jsonl_entry() after upconversion.
# ══════════════════════════════════════════════════════════════════════

class TestEnrichmentPopulatesFields:
    """Semantic tiles arrive with title, preview, tags populated from graph.db.

    Expected: FAIL — _enrich_semantic_tile() doesn't exist yet.
    """

    def test_note_created_gets_title(self, test_graph_db, monkeypatch):
        """tool_result 'Note saved (src:abc)' → entry has title from graph.db.

        Expected: FAIL (no enrichment function exists).
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        result = _semantic(_parse_jsonl_entry(_line(_make_tool_result_note_saved("a1b2c3d4-0001"))))
        assert result is not None
        assert result["type"] == "semantic_bash"
        assert result["semantic_type"] == "note-created"
        # Enrichment should populate title from graph.db sources table
        assert "title" in result, (
            "Enriched note-created entry should have 'title' field. "
            "Requires _enrich_semantic_tile() in server.py."
        )
        assert result["title"] == "Session Viewer Redesign", (
            "Title should be stripped of leading '# ' heading marker"
        )

    def test_note_created_gets_preview(self, test_graph_db, monkeypatch):
        """tool_result 'Note saved (src:abc)' → preview is first 120 chars of content.

        Expected: FAIL (no enrichment function exists).
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        result = _semantic(_parse_jsonl_entry(_line(_make_tool_result_note_saved("a1b2c3d4-0001"))))
        assert result is not None
        assert "preview" in result, (
            "Enriched note-created entry should have 'preview' field"
        )
        # Preview should skip heading lines and contain content body
        assert "architecture" in result["preview"].lower() or "unified session" in result["preview"].lower(), (
            f"Preview should contain body content, got: {result.get('preview', '')}"
        )
        assert len(result["preview"]) <= 120, "Preview should be at most 120 chars"

    def test_note_created_gets_tags(self, test_graph_db, monkeypatch):
        """tool_result 'Note saved (src:abc)' → tags from source metadata JSON.

        Expected: FAIL (no enrichment function exists).
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        result = _semantic(_parse_jsonl_entry(_line(_make_tool_result_note_saved("a1b2c3d4-0001"))))
        assert result is not None
        assert "tags" in result, (
            "Enriched note-created entry should have 'tags' field"
        )
        assert isinstance(result["tags"], list)
        assert "architecture" in result["tags"]
        assert "session-viewer" in result["tags"]

    def test_thought_captured_enriched(self, test_graph_db, monkeypatch):
        """'✓ Captured:' result → title, preview, tags populated.

        Expected: FAIL (no enrichment function exists).
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        result = _semantic(_parse_jsonl_entry(_line(_make_tool_result_thought_captured("b2c3d4e5-0002"))))
        assert result is not None
        assert result["type"] == "semantic_bash"
        assert result["semantic_type"] == "thought-captured"
        # All three enrichment fields should be present
        assert "title" in result, "Enriched thought should have 'title'"
        assert "preview" in result, "Enriched thought should have 'preview'"
        assert "tags" in result, "Enriched thought should have 'tags'"
        assert result["title"] == "auth needs passkeys"
        assert "auth" in result["tags"]

    def test_comment_added_enriched(self, test_graph_db, monkeypatch):
        """'Comment added' result → title, preview, tags from parent note.

        Expected: FAIL (no enrichment function exists).
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        result = _semantic(_parse_jsonl_entry(_line(
            _make_tool_result_comment_added("d4e5f6a7-0004", "c3d4e5f6-0003")
        )))
        assert result is not None
        assert result["type"] == "semantic_bash"
        assert result["semantic_type"] == "comment-added"
        # Comment enrichment should use the PARENT note's metadata
        assert "title" in result, "Enriched comment should have 'title' from parent note"
        assert "tags" in result, "Enriched comment should have 'tags' from parent note"


# ══════════════════════════════════════════════════════════════════════
# TestEnrichmentFallback — graceful degradation when graph.db unavailable
#
# Expected: PASS on current code — without enrichment, entries already
# render with raw content. These tests verify the current fallback
# behavior is preserved.
# ══════════════════════════════════════════════════════════════════════

class TestEnrichmentFallback:
    """Entries render with raw content when enrichment is unavailable.

    Expected: PASS — this is current behavior.
    """

    def test_graph_db_unavailable(self, monkeypatch, tmp_path):
        """GRAPH_DB points to nonexistent file → entry has raw content, no crash.

        Expected: PASS — current code doesn't read graph.db during parsing.
        """
        monkeypatch.setenv("GRAPH_DB", str(tmp_path / "nonexistent.db"))
        result = _semantic(_parse_jsonl_entry(_line(_make_tool_result_note_saved("a1b2c3d4-0001"))))
        assert result is not None
        assert result["type"] == "semantic_bash"
        assert result["semantic_type"] == "note-created"
        # Should have content from the raw CLI output
        assert "content" in result
        assert "Note saved" in result["content"] or "a1b2c3d4-0001" in result.get("source_id", "")

    def test_source_not_found(self, test_graph_db, monkeypatch):
        """source_id not in graph.db → entry has raw content, no crash.

        Expected: PASS — current code doesn't try to look up the source.
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        result = _semantic(_parse_jsonl_entry(_line(
            _make_tool_result_note_saved("0000dead-beef-0000")
        )))
        assert result is not None
        assert result["type"] == "semantic_bash"
        # Entry should still be valid with raw content
        assert "content" in result
        assert result["source_id"] == "0000dead-beef-0000"

    def test_non_semantic_entries_untouched(self, test_graph_db, monkeypatch):
        """Regular tool_result → no title/preview/tags added.

        Expected: PASS — regular entries are never enriched.
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        result = _parse_jsonl_entry(_line(_make_regular_tool_result()))
        assert result is not None
        assert result["type"] == "tool_result"
        # No enrichment fields on regular entries
        assert "title" not in result
        assert "preview" not in result
        assert "tags" not in result


# ══════════════════════════════════════════════════════════════════════
# TestEnrichmentCodePath — same enrichment for live and backfill
#
# Expected: FAIL on current code (enrichment doesn't exist).
# ══════════════════════════════════════════════════════════════════════

class TestEnrichmentCodePath:
    """Enrichment is path-independent: same JSONL line → same enriched entry.

    Expected: FAIL — enrichment function doesn't exist.
    """

    def test_same_output_live_and_backfill(self, test_graph_db, monkeypatch):
        """Same JSONL line through live parse and backfill parse → identical entry.

        Expected: FAIL (enrichment not implemented).

        Spec Section 3d: 'Same code path for live SSE tailing and backfill.
        Tiles arrive at the client fully enriched.'
        Both paths call _parse_jsonl_entry(), so if enrichment is in that
        function, the output is automatically identical.
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        line = _line(_make_tool_result_note_saved("a1b2c3d4-0001"))

        # Parse the same line twice (simulating live vs backfill)
        result1 = _semantic(_parse_jsonl_entry(line))
        result2 = _semantic(_parse_jsonl_entry(line))

        assert result1 is not None
        assert result2 is not None
        assert result1 == result2, (
            "Same JSONL line must produce identical entries regardless of "
            "whether it's parsed during live tailing or backfill"
        )
        # Both should have enrichment fields (this is the FAIL part)
        assert "title" in result1, (
            "Entries should be enriched with title from graph.db"
        )

    def test_enrichment_in_user_tool_result_block(self, test_graph_db, monkeypatch):
        """note-created inside user message content list → also enriched.

        Expected: FAIL (enrichment not implemented).

        JSONL entries can contain note-created results in two formats:
        1. Top-level tool_result (tested above)
        2. Inside a user message's content list (this test)

        Both paths must produce enriched entries.
        """
        monkeypatch.setenv("GRAPH_DB", test_graph_db)
        line = _line(_make_user_list_with_note_saved("a1b2c3d4-0001"))
        result = _semantic(_parse_jsonl_entry(line))

        assert result is not None
        assert result["type"] == "semantic_bash"
        assert result["semantic_type"] == "note-created"
        # Enrichment should work for entries from user content lists too
        assert "title" in result, (
            "note-created inside user content list should also be enriched"
        )
