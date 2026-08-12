"""The composed Mission screen.

The point of these tests is the two guarantees the epic rests on: the
author's document survives composition untouched, and both surfaces get
the same bytes because there is only one function that builds them.
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from unittest.mock import MagicMock, patch

from tools.dashboard import link_serving
from tools.dashboard.dao import mission_control_db as db
from tools.dashboard.plugins.mission_control import compose


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "mission_control.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)


@pytest.fixture(autouse=True)
def _no_real_presence_writes():
    """Same reason test_api.py autouses this: push_site_revision fires a
    best-effort coordinator heartbeat through the real graph Settings
    substrate, which this module's tmp_path DB does not isolate."""
    with patch("tools.graph.surface.Presence", MagicMock()):
        yield


def _state_of(document: str) -> dict:
    block = re.search(
        r'<script type="application/json" id="mc-state">(.*?)</script>',
        document, re.S,
    )
    assert block, "composed screen has no state block"
    return json.loads(block.group(1))


def test_the_authors_document_is_carried_byte_for_byte():
    """The whole epic exists to stop rewriting author markup.

    This document is deliberately hostile to a rewriter: an unclosed tag,
    a fragment link, a script that would break under reserialisation, and
    an attribute a naive sanitizer strips.
    """
    author = (
        '<html><body><a href="#deep">jump</a>'
        '<p class=unquoted>text<br>'
        '<script>var re = /<\\/p>/; if (1 < 2) document.title = "x";</script>'
        '<h2 id="deep">there</h2></body></html>'
    )
    mission = db.create_mission("Hostile")
    db.push_site_revision(mission["mission_id"], author, "first")

    document = compose.compose_screen(mission["mission_id"]).decode("utf-8")
    assert author in document
    # ...and it is the TAIL: nothing is appended after the author's page.
    assert document.endswith(author)


def test_both_surfaces_produce_identical_bytes():
    """The relay resolver and the dashboard path call ONE function.

    If this ever diverges, a guest and a coordinator are looking at
    different documents while believing they are looking at the same one.
    """
    mission = db.create_mission("Two surfaces")
    mission_id = mission["mission_id"]
    db.push_site_revision(mission_id, "<html>page</html>", "first")

    from tools.dashboard.plugins.mission_control.entrypoints import api as mc_api

    direct = compose.compose_screen(mission_id)
    via_resolver = link_serving._resolve_mission(mission_id)["viewer"]
    assert via_resolver == direct

    pillar = db.create_pillar(mission_id, "P", "sess", "#4ade80")
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>pillar</html>", "first")
    over_channel = asyncio.run(mc_api.handle_relay_read(
        "guest:1", mission_id,
        {"kind": "pillar_site", "pillar_id": pillar["pillar_id"]},
    ))["document"]
    assert over_channel == compose.compose_screen(
        mission_id, pillar["pillar_id"]
    ).decode("utf-8")


def test_the_screen_carries_the_state_the_chrome_renders():
    mission = db.create_mission("Stateful")
    mission_id = mission["mission_id"]
    db.push_site_revision(mission_id, "<html>page</html>", "first")
    pillar = db.create_pillar(mission_id, "Delivery", "sess-del", "#38bdf8")
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>p</html>", "first")
    db.ask_question(mission_id, "Which pattern?", "guest:1", "Jeremy",
                    pillar_id=pillar["pillar_id"], anchor="table:x")

    state = _state_of(compose.compose_screen(mission_id).decode("utf-8"))
    assert state["screen"] is None
    assert [p["name"] for p in state["pillars"]] == ["Delivery"]
    assert state["pillars"][0]["open"] == 1
    assert state["pillars"][0]["color"] == "#38bdf8"
    assert state["questions"][0]["question"] == "Which pattern?"
    assert state["questions"][0]["anchor"] == "table:x"

    on_pillar = _state_of(
        compose.compose_screen(mission_id, pillar["pillar_id"]).decode("utf-8")
    )
    assert on_pillar["screen"] == pillar["pillar_id"]


def test_a_question_cannot_close_the_state_block():
    """Question text is untrusted input and lands inside a <script> block.

    ``</script`` is the one sequence an HTML parser acts on there, and it
    matches case-insensitively, so a question containing it must not be
    able to end the block early and start executing.
    """
    mission = db.create_mission("Injection")
    mission_id = mission["mission_id"]
    db.push_site_revision(mission_id, "<html>page</html>", "first")
    db.ask_question(mission_id, "</SCRIPT><img src=x onerror=alert(1)>",
                    "guest:1", "Jeremy")

    document = compose.compose_screen(mission_id).decode("utf-8")
    head = document.split("<html>page</html>")[0]
    assert "</SCRIPT>" not in head and "</script><img" not in head
    # Still valid JSON, and the text round-trips intact.
    state = _state_of(document)
    assert state["questions"][0]["question"] == "</SCRIPT><img src=x onerror=alert(1)>"


def test_a_mission_with_no_revision_composes_nothing():
    mission = db.create_mission("Empty")
    assert compose.compose_screen(mission["mission_id"]) is None


def test_the_base_element_precedes_the_authors_markup():
    """<base href="about:srcdoc"> is what makes #fragment links resolve
    in-document in a sandboxed frame, and a <base> only counts if it comes
    before anything that resolves a URL."""
    mission = db.create_mission("Base")
    db.push_site_revision(mission["mission_id"], '<a href="#x">x</a>', "first")
    document = compose.compose_screen(mission["mission_id"]).decode("utf-8")
    assert document.index('<base href="about:srcdoc">') < document.index('href="#x"')


def test_the_pillar_status_line_is_passed_through_verbatim():
    """The one field on that row a human wrote. It is never substituted
    for, and a pillar without one renders nothing rather than a guess."""
    mission = db.create_mission("Status")
    mission_id = mission["mission_id"]
    db.push_site_revision(mission_id, "<html>page</html>", "first")
    written = db.create_pillar(mission_id, "Collection", "sess", "#4ade80")
    silent = db.create_pillar(mission_id, "Delivery", "sess2", "#38bdf8")
    db.set_pillar_last_done(
        written["pillar_id"],
        "Ran the prototype over a full batch and measured how fast it goes. "
        "Found and fixed two bugs in a library we depend on.",
    )

    by_name = {p["name"]: p for p in
               _state_of(compose.compose_screen(mission_id).decode("utf-8"))["pillars"]}
    assert by_name["Collection"]["last_done"].startswith("Ran the prototype")
    # Not the status value, not a revision note, not an empty string: None.
    assert by_name["Delivery"]["last_done"] is None


def test_the_overview_screen_carries_the_missions_name():
    """The bar is where a reader confirms which mission they are in. On the
    overview there is no pillar to name it, and the literal "Mission" told
    them nothing -- which is what the live link showed."""
    mission = db.create_mission("OSS Insights")
    db.push_site_revision(mission["mission_id"], "<html>page</html>", "first")
    state = _state_of(compose.compose_screen(mission["mission_id"]).decode("utf-8"))
    assert state["mission"] == "OSS Insights"


def test_a_retired_question_leaves_the_screen_but_not_the_record():
    mission = db.create_mission("Retire")
    mission_id = mission["mission_id"]
    db.push_site_revision(mission_id, "<html>page</html>", "first")
    kept = db.ask_question(mission_id, "still matters", "guest:1", "Jeremy")
    gone = db.ask_question(mission_id, "about a view that no longer exists",
                           "guest:1", "Jeremy")
    db.retire_question(gone["entry_id"], "that view was replaced")

    state = _state_of(compose.compose_screen(mission_id).decode("utf-8"))
    assert [q["question"] for q in state["questions"]] == ["still matters"]
    # ...and it is still there for anyone reading the record.
    assert db.get_conversation_entry(gone["entry_id"])["question"].startswith("about a view")
