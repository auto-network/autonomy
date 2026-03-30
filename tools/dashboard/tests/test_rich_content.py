"""L1 unit tests for rich-content notes and alt-text attachments.

Tests cover:
- Alt-text storage on attachments (--alt, --alt-file)
- Rich-content note creation (--html)
- Rich-content note update with dual enforcement
- Version-paired HTML attachments
- ![[id]] embed resolution in graph read
- Comment cascading for embedded child notes
- Backwards compatibility with ![alt](graph://id)
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# ── Helpers ────────────────────────────────────────────────────────

def _clean_env():
    """Return env dict with API mode and session vars stripped for local-only testing."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3])}
    # Force local DB mode (not API)
    env.pop("GRAPH_API", None)
    # Disable auto-provenance
    env.pop("CLAUDE_SESSION_ID", None)
    return env


def _ensure_read_gate_marker():
    """Create the read-gate marker file for the note revision protocol."""
    marker_dir = Path.home() / ".graph" / "reads"
    marker_dir.mkdir(parents=True, exist_ok=True)
    # Create markers matching the prefix 843a8137 (Note Revision Protocol)
    (marker_dir / "843a8137-test-gate-bypass").touch()


def _graph_cmd(*args, stdin_text=None, db_path=None, bypass_read_gate=False):
    """Run a graph CLI command, return (returncode, stdout, stderr)."""
    cmd = [sys.executable, "-m", "tools.graph.cli"]
    if db_path:
        cmd.extend(["--db", str(db_path)])
    cmd.extend(args)
    env = _clean_env()
    if bypass_read_gate:
        _ensure_read_gate_marker()
    result = subprocess.run(
        cmd, capture_output=True, text=True, input=stdin_text, env=env, timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


def _extract_src_id(output):
    """Extract source ID from CLI output like 'src:abcdef01-234'."""
    import re
    m = re.search(r'src:([a-f0-9]+-[a-f0-9]+)', output)
    assert m, f"Could not find source ID in: {output}"
    return m.group(1)


def _extract_att_id(output):
    """Extract attachment ID from CLI output like '(abcdef01-234'."""
    import re
    m = re.search(r'\(([a-f0-9]+-[a-f0-9]+)', output)
    assert m, f"Could not find attachment ID in: {output}"
    return m.group(1)


@pytest.fixture
def graph_db(tmp_path):
    """Create a fresh graph DB for testing."""
    db_path = tmp_path / "test_graph.db"
    from tools.graph.db import GraphDB
    db = GraphDB(db_path)
    db.close()
    return db_path


@pytest.fixture
def html_file(tmp_path):
    """Create a sample HTML file for rich-content tests."""
    p = tmp_path / "diagram.html"
    p.write_text("<html><body><h1>Pause Mechanisms</h1><div class='card'>Global</div></body></html>")
    return p


@pytest.fixture
def markdown_file(tmp_path):
    """Create a sample markdown file for note content."""
    p = tmp_path / "desc.md"
    p.write_text("# Pause Mechanisms\n\n| Scope | Trigger |\n|-------|--------|\n| Global | Auth failure |")
    return p


@pytest.fixture
def image_file(tmp_path):
    """Create a minimal PNG file."""
    p = tmp_path / "screenshot.png"
    import base64
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    p.write_bytes(png_data)
    return p


# ── Alt-text storage ──────────────────────────────────────────────

class TestAltTextStorage:
    def test_attach_with_alt(self, graph_db, image_file):
        """graph attach --alt stores alt_text in DB."""
        rc, out, err = _graph_cmd(
            "attach", str(image_file), "--alt", "Dispatch page with 3 running beads",
            db_path=graph_db,
        )
        assert rc == 0, f"Failed: {err}"
        assert "Attached" in out

        from tools.graph.db import GraphDB
        db = GraphDB(graph_db)
        atts = db.list_attachments()
        assert len(atts) >= 1
        att = atts[0]
        assert att["alt_text"] == "Dispatch page with 3 running beads"
        db.close()

    def test_attach_with_alt_file(self, graph_db, image_file, tmp_path):
        """graph attach --alt-file reads alt-text from file."""
        alt_path = tmp_path / "alt.md"
        alt_path.write_text("Dispatch page showing the active dispatches section\nwith 3 running beads.")

        rc, out, err = _graph_cmd(
            "attach", str(image_file), "--alt-file", str(alt_path),
            db_path=graph_db,
        )
        assert rc == 0, f"Failed: {err}"

        from tools.graph.db import GraphDB
        db = GraphDB(graph_db)
        atts = db.list_attachments()
        assert len(atts) >= 1
        att = atts[0]
        assert "3 running beads" in att["alt_text"]
        db.close()


# ── Rich-content note creation ────────────────────────────────────

class TestRichContentCreation:
    def test_note_with_html_creates_rich_content(self, graph_db, html_file, markdown_file):
        """graph note --html creates note with rich_content: true in metadata."""
        md_text = markdown_file.read_text()
        rc, out, err = _graph_cmd(
            "note", "-c", "-", "--html", str(html_file), "--tags", "test",
            db_path=graph_db, stdin_text=md_text,
        )
        assert rc == 0, f"Failed: {err}"
        assert "rich-content" in out
        assert "Note saved" in out

        from tools.graph.db import GraphDB
        db = GraphDB(graph_db)
        sources = db.conn.execute("SELECT * FROM sources WHERE type='note'").fetchall()
        assert len(sources) == 1
        source = dict(sources[0])
        meta = json.loads(source["metadata"])
        assert meta["rich_content"] is True

        # Note content is the markdown, not the HTML
        thoughts = db.get_thoughts_by_source(source["id"])
        assert len(thoughts) == 1
        assert "Pause Mechanisms" in thoughts[0]["content"]
        assert "<html>" not in thoughts[0]["content"]

        # HTML attachment exists with version-paired source_id
        atts = db.conn.execute(
            "SELECT * FROM attachments WHERE source_id LIKE ?",
            (f"{source['id']}@%",)
        ).fetchall()
        assert len(atts) == 1
        att = dict(atts[0])
        assert att["source_id"] == f"{source['id']}@1"
        assert att["mime_type"] == "text/html"

        # Version 1 was stored
        v1 = db.get_note_version(source["id"], 1)
        assert v1 is not None
        assert "Pause Mechanisms" in v1["content"]

        db.close()


# ── Rich-content note update ──────────────────────────────────────

class TestRichContentUpdate:
    def _create_rich_note(self, graph_db, html_file, markdown_file):
        """Helper to create a rich-content note and return its source_id."""
        md_text = markdown_file.read_text()
        rc, out, err = _graph_cmd(
            "note", "-c", "-", "--html", str(html_file), "--tags", "test",
            db_path=graph_db, stdin_text=md_text,
        )
        assert rc == 0, f"Create failed: {err}"
        return _extract_src_id(out)

    def test_update_without_html_rejected(self, graph_db, html_file, markdown_file):
        """Updating rich-content note without --html is rejected."""
        src_id = self._create_rich_note(graph_db, html_file, markdown_file)

        rc, out, err = _graph_cmd(
            "note", "update", src_id, "-c", "-",
            db_path=graph_db, stdin_text="Updated markdown",
            bypass_read_gate=True,
        )
        assert rc != 0 or "rich-content note requires --html" in err, \
            f"Should reject: rc={rc} out={out} err={err}"

    def test_update_with_html_creates_new_version(self, graph_db, html_file, markdown_file, tmp_path):
        """Updating with --html creates version 2 and new attachment."""
        src_id = self._create_rich_note(graph_db, html_file, markdown_file)

        html_v2 = tmp_path / "diagram_v2.html"
        html_v2.write_text("<html><body><h1>Updated Pause Mechanisms</h1></body></html>")

        rc, out, err = _graph_cmd(
            "note", "update", src_id, "-c", "-", "--html", str(html_v2),
            db_path=graph_db, stdin_text="# Updated Pause Mechanisms\n\nNew content.",
            bypass_read_gate=True,
        )
        assert rc == 0, f"Update failed: {err}"
        assert "version 2" in out

        from tools.graph.db import GraphDB
        db = GraphDB(graph_db)

        source = db.get_source(src_id)
        full_id = source["id"]

        # v1 attachment still exists
        v1_att = db.conn.execute(
            "SELECT * FROM attachments WHERE source_id = ?",
            (f"{full_id}@1",)
        ).fetchone()
        assert v1_att is not None

        # v2 attachment exists
        v2_att = db.conn.execute(
            "SELECT * FROM attachments WHERE source_id = ?",
            (f"{full_id}@2",)
        ).fetchone()
        assert v2_att is not None

        assert dict(v1_att)["id"] != dict(v2_att)["id"]

        v1 = db.get_note_version(full_id, 1)
        assert "Pause Mechanisms" in v1["content"]
        v2 = db.get_note_version(full_id, 2)
        assert "Updated Pause Mechanisms" in v2["content"]

        db.close()


# ── graph read embed resolution ───────────────────────────────────

class TestReadEmbedResolution:
    def test_embed_rich_content_note(self, graph_db, html_file, markdown_file):
        """![[id]] in parent note resolves to child note's markdown with attachment marker."""
        md_text = markdown_file.read_text()

        # Create the rich-content child note
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--html", str(html_file), "--tags", "test",
            db_path=graph_db, stdin_text=md_text,
        )
        assert rc == 0
        child_id = _extract_src_id(out)

        # Create parent note with ![[child-id]] embed
        parent_text = f"# Architecture Overview\n\n## Pause Mechanisms\n\n![[{child_id}]]\n\nThe pause system has three scopes."
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--tags", "test",
            db_path=graph_db, stdin_text=parent_text,
        )
        assert rc == 0
        parent_id = _extract_src_id(out)

        # Read parent note — should resolve embed
        rc, out, err = _graph_cmd("read", parent_id, "--first", db_path=graph_db)
        assert rc == 0, f"Read failed: {err}"
        assert "[attachment(text/html):" in out
        assert "Pause Mechanisms" in out
        assert "The pause system has three scopes" in out

    def test_embed_image_attachment(self, graph_db, image_file):
        """![[att-id]] resolves to attachment's alt_text with marker."""
        rc, out, _ = _graph_cmd(
            "attach", str(image_file), "--alt", "Screenshot of dispatch page",
            db_path=graph_db,
        )
        assert rc == 0
        att_id = _extract_att_id(out)

        note_text = f"# Dashboard\n\n![[{att_id}]]\n\nAbove shows the dispatch page."
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--tags", "test",
            db_path=graph_db, stdin_text=note_text,
        )
        assert rc == 0
        note_id = _extract_src_id(out)

        rc, out, err = _graph_cmd("read", note_id, "--first", db_path=graph_db)
        assert rc == 0, f"Read failed: {err}"
        assert "[attachment(image/png):" in out
        assert "Screenshot of dispatch page" in out

    def test_embed_no_alt_text(self, graph_db, image_file):
        """Attachment without alt-text emits marker line only."""
        rc, out, _ = _graph_cmd(
            "attach", str(image_file),
            db_path=graph_db,
        )
        assert rc == 0
        att_id = _extract_att_id(out)

        note_text = f"See: ![[{att_id}]]"
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--tags", "test", "--force",
            db_path=graph_db, stdin_text=note_text,
        )
        assert rc == 0
        note_id = _extract_src_id(out)

        rc, out, _ = _graph_cmd("read", note_id, "--first", db_path=graph_db)
        assert rc == 0
        assert "[attachment(image/png):" in out

    def test_backwards_compat_graph_uri(self, graph_db, image_file):
        """Existing ![alt](graph://id) syntax still works."""
        rc, out, _ = _graph_cmd(
            "attach", str(image_file),
            db_path=graph_db,
        )
        assert rc == 0
        att_id = _extract_att_id(out)

        note_text = f"See: ![screenshot](graph://{att_id})"
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--tags", "test", "--force",
            db_path=graph_db, stdin_text=note_text,
        )
        assert rc == 0
        note_id = _extract_src_id(out)

        rc, out, _ = _graph_cmd("read", note_id, "--first", db_path=graph_db)
        assert rc == 0
        # Old syntax should be preserved as-is (not broken by ![[]] parsing)
        assert f"graph://{att_id}" in out


# ── graph read --html ──────────────────────────────────────────────

class TestReadHtml:
    def test_read_html_outputs_raw_source(self, graph_db, html_file, markdown_file):
        """graph read <rich-note> --html outputs raw HTML source."""
        md_text = markdown_file.read_text()
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--html", str(html_file), "--tags", "test",
            db_path=graph_db, stdin_text=md_text,
        )
        assert rc == 0
        src_id = _extract_src_id(out)

        rc, out, err = _graph_cmd("read", src_id, "--first", "--html", db_path=graph_db)
        assert rc == 0, f"Failed: {err}"
        assert "<html>" in out
        assert "Pause Mechanisms" in out
        assert "<div class='card'>" in out


# ── Comment cascading ──────────────────────────────────────────────

class TestCommentCascading:
    def test_all_comments_includes_embedded_child(self, graph_db, html_file, markdown_file):
        """graph read <parent> --all-comments includes child note comments."""
        md_text = markdown_file.read_text()

        # Create child note
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--html", str(html_file), "--tags", "test",
            db_path=graph_db, stdin_text=md_text,
        )
        assert rc == 0
        child_id = _extract_src_id(out)

        # Add comment to child note
        from tools.graph.db import GraphDB
        db = GraphDB(graph_db)
        child_source = db.get_source(child_id)
        assert child_source is not None, f"Child source not found: {child_id}"
        db.insert_comment(child_source["id"], "Fix the Global row description")
        db.close()

        # Create parent with embed
        parent_text = f"# Overview\n\n![[{child_id}]]"
        rc, out, _ = _graph_cmd(
            "note", "-c", "-", "--tags", "test",
            db_path=graph_db, stdin_text=parent_text,
        )
        assert rc == 0
        parent_id = _extract_src_id(out)

        rc, out, err = _graph_cmd("read", parent_id, "--first", "--all-comments", db_path=graph_db)
        assert rc == 0, f"Failed: {err}"
        assert "Fix the Global row description" in out
        assert f"Comments on ![[{child_id}]]" in out

    def test_no_embeds_unchanged(self, graph_db):
        """--all-comments on note without embeds behaves unchanged."""
        rc, out, _ = _graph_cmd(
            "note", "Simple note with no embeds", "--tags", "test",
            db_path=graph_db,
        )
        assert rc == 0
        note_id = _extract_src_id(out)

        from tools.graph.db import GraphDB
        db = GraphDB(graph_db)
        source = db.get_source(note_id)
        assert source is not None
        db.insert_comment(source["id"], "A direct comment")
        db.close()

        rc, out, _ = _graph_cmd("read", note_id, "--first", "--all-comments", db_path=graph_db)
        assert rc == 0
        assert "A direct comment" in out
        assert "Comments on ![[" not in out


# ── Alt-text in attachment metadata display ────────────────────────

class TestAttachmentDisplay:
    def test_attachment_shows_alt_text(self, graph_db, image_file):
        """graph attachment <id> shows alt_text field."""
        rc, out, _ = _graph_cmd(
            "attach", str(image_file), "--alt", "A description of the image",
            db_path=graph_db,
        )
        assert rc == 0
        att_id = _extract_att_id(out)

        rc, out, err = _graph_cmd("attachment", att_id, db_path=graph_db)
        assert rc == 0, f"Failed: {err}"
        assert "alt_text:" in out
        assert "A description" in out
