"""The source header card renders what the response actually carries.

Two fields the card is built around were never arriving. The organization
reached the viewer as a bare slug string where the template renders an
identity — a colour, an initial, a name — so the glyph drew nothing. And the
note's version count was absent entirely, so the ``@vN`` chip never appeared
and a reader could not see which revision they were looking at.
"""
from __future__ import annotations

import pytest

from tools.dashboard import server
from tools.graph.db import GraphDB


@pytest.fixture
def orgs(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def test_a_stored_org_slug_is_resolved_to_its_identity():
    """The row carries a slug; the card renders an identity.

    A presence check never resolves it, because the column is always there —
    the string simply arrives where an object is expected, and every field
    the glyph reads comes back undefined.
    """
    result = {"source": {"id": "abc", "type": "note", "org": "autonomy"}}

    server._attach_source_org(result)

    org = result["source"]["org"]
    assert isinstance(org, dict), "the viewer renders org.color / org.initial"
    assert org["slug"] == "autonomy"
    assert org["initial"] and org["color"]


def test_an_already_resolved_org_is_left_alone():
    resolved = {"slug": "autonomy", "name": "Autonomy", "color": "#118AB2",
                "initial": "A", "favicon": None, "resolved": True}
    result = {"source": {"id": "abc", "type": "note", "org": dict(resolved)}}

    server._attach_source_org(result)

    assert result["source"]["org"] == resolved


def test_a_source_with_no_org_still_resolves_to_something_renderable():
    result = {"source": {"id": "abc", "type": "note"}}

    server._attach_source_org(result)

    org = result["source"]["org"]
    assert isinstance(org, dict)
    assert "initial" in org and "color" in org


def test_version_count_reports_the_revisions_a_note_has(orgs):
    db = GraphDB(org="acme")
    try:
        db.conn.execute(
            "INSERT INTO sources(id, type, title) VALUES('n1', 'note', 'T')")
        for version in (1, 2, 3):
            db.insert_note_version("n1", version, f"body {version}")
        db.conn.commit()
    finally:
        db.close()

    assert server._note_version_count("n1", "acme") == 3


def test_a_note_never_revised_counts_as_one(orgs):
    db = GraphDB(org="acme")
    try:
        db.conn.execute(
            "INSERT INTO sources(id, type, title) VALUES('n2', 'note', 'T')")
        db.conn.commit()
    finally:
        db.close()

    assert server._note_version_count("n2", "acme") == 1


def test_an_unreachable_database_does_not_break_the_page(orgs):
    """The chip is worth less than the page it sits on."""
    assert server._note_version_count("n1", "never-provisioned") == 1
