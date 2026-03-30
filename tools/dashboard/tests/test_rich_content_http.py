"""L2.A HTTP contract tests for rich-content notes and embed resolution.

Tests use Starlette TestClient against the dashboard server with a test graph DB.
"""
import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient


# ── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def test_graph_db(tmp_path):
    """Create a test graph DB with rich-content notes and image attachments."""
    import base64
    import hashlib
    import shutil

    db_path = tmp_path / "graph.db"
    from tools.graph.db import GraphDB
    from tools.graph.models import Source, Thought, Attachment, new_id

    db = GraphDB(db_path)

    # ── Rich-content note ──
    rc_source = Source(
        type="note", platform="local", project="test",
        title="Pause Mechanisms",
        file_path=f"note:{new_id()}",
        metadata={"tags": ["test"], "author": "user", "rich_content": True},
    )
    db.insert_source(rc_source)

    rc_thought = Thought(
        source_id=rc_source.id,
        content="# Pause Mechanisms\n\n| Scope | Trigger |\n|-------|--------|\n| Global | Auth failure |",
        role="user", turn_number=1, tags=["test"],
    )
    db.insert_thought(rc_thought)
    db.insert_note_version(rc_source.id, 1, rc_thought.content)

    # HTML attachment for version 1
    html_content = b"<html><body><h1>Pause Mechanisms</h1><div class='card'>Global</div></body></html>"
    html_hash = hashlib.sha256(html_content).hexdigest()
    html_dir = tmp_path / "attachments" / html_hash[:2]
    html_dir.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / f"{html_hash}.html"
    html_path.write_bytes(html_content)

    html_att = Attachment(
        hash=html_hash, filename="diagram.html", mime_type="text/html",
        size_bytes=len(html_content), file_path=str(html_path),
        source_id=f"{rc_source.id}@1",
    )
    db.insert_attachment(html_att)

    # ── Image attachment with alt-text ──
    png_data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    img_hash = hashlib.sha256(png_data).hexdigest()
    img_dir = tmp_path / "attachments" / img_hash[:2]
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / f"{img_hash}.png"
    img_path.write_bytes(png_data)

    img_att = Attachment(
        hash=img_hash, filename="screenshot.png", mime_type="image/png",
        size_bytes=len(png_data), file_path=str(img_path),
        source_id=rc_source.id,
        alt_text="Dispatch page showing the active dispatches section with 3 running beads",
    )
    db.insert_attachment(img_att)

    db.commit()
    db.close()

    return {
        "db_path": db_path,
        "rich_note_id": rc_source.id,
        "html_att_id": html_att.id,
        "img_att_id": img_att.id,
    }


@pytest.fixture
def test_client(test_graph_db, monkeypatch):
    """Create a test client with the test graph DB."""
    db_path = test_graph_db["db_path"]

    # Patch GraphDB to use our test DB
    original_init = None
    from tools.graph import db as graph_db_mod

    original_init = graph_db_mod.GraphDB.__init__

    def patched_init(self, db_path_arg=None, **kwargs):
        original_init(self, db_path or db_path_arg, **kwargs)

    monkeypatch.setattr(graph_db_mod.GraphDB, "__init__",
                        lambda self, db_path_arg=None, **kw: original_init(self, db_path, **kw))

    # Reload server to pick up patches
    from tools.dashboard import server
    importlib.reload(server)

    with TestClient(server.app) as client:
        yield client

    # Restore
    importlib.reload(server)


# ── Tests ──────────────────────────────────────────────────────────

class TestAttachmentServing:
    def test_html_attachment_serves_with_correct_type(self, test_client, test_graph_db):
        """GET /api/attachment/<html-id> returns Content-Type: text/html."""
        att_id = test_graph_db["html_att_id"][:12]
        resp = test_client.get(f"/api/attachment/{att_id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<html>" in resp.text

    def test_image_attachment_unchanged(self, test_client, test_graph_db):
        """Existing image attachment serving unchanged."""
        att_id = test_graph_db["img_att_id"][:12]
        resp = test_client.get(f"/api/attachment/{att_id}")
        assert resp.status_code == 200
        assert "image/png" in resp.headers["content-type"]


class TestResolveEndpoint:
    def test_resolve_rich_content_note(self, test_client, test_graph_db):
        """GET /api/resolve/<rich-note-id> returns rich-content type with attachment URL."""
        note_id = test_graph_db["rich_note_id"][:12]
        resp = test_client.get(f"/api/resolve/{note_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "rich-content"
        assert data["mime_type"] == "text/html"
        assert data["attachment_url"] is not None
        assert "/api/attachment/" in data["attachment_url"]
        assert "Pause Mechanisms" in data["alt_text"]

    def test_resolve_image_attachment(self, test_client, test_graph_db):
        """GET /api/resolve/<image-att-id> returns attachment type with alt-text."""
        att_id = test_graph_db["img_att_id"][:12]
        resp = test_client.get(f"/api/resolve/{att_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "attachment"
        assert data["mime_type"] == "image/png"
        assert "/api/attachment/" in data["attachment_url"]
        assert "3 running beads" in data["alt_text"]

    def test_resolve_rich_content_with_version(self, test_client, test_graph_db):
        """GET /api/resolve/<rich-note-id>?version=1 returns version 1's attachment URL."""
        note_id = test_graph_db["rich_note_id"][:12]
        resp = test_client.get(f"/api/resolve/{note_id}?version=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "rich-content"
        assert data["attachment_url"] is not None

    def test_resolve_not_found(self, test_client):
        """GET /api/resolve/<nonexistent> returns 404."""
        resp = test_client.get("/api/resolve/00000000-000")
        assert resp.status_code == 404
