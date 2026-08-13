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

    # The relay path is FRAMED and carries <base href="about:srcdoc">; the
    # dashboard path is served at a real URL and must not, or every relative
    # link in the author's content resolves against about:srcdoc and the
    # browser blocks it. One function still builds both, which is the property
    # that matters -- the author's content and the state are identical.
    # The resolver also tells the document whether this channel may write, so
    # compare against the same call it makes: a grant with a bound participant.
    grant = {"meta": {"participant_id": "guest:someone"}}
    # viewer, like may_write, is a property of the READER and legitimately
    # differs between two people looking at the same screen. Pass the grant's
    # own participant so both sides describe the same reader; what must not
    # drift is everything else.
    framed = compose.compose_screen(
        mission_id, framed=True, may_write=True, viewer="guest:someone")
    via_resolver = link_serving._resolve_mission(mission_id, grant)["viewer"]
    assert via_resolver == framed
    assert b'<base href="about:srcdoc">' in framed

    unframed = compose.compose_screen(mission_id, viewer="guest:someone")
    # Scoped to the HEAD: the bootstrap's own comments mention the tag by
    # name, so "not anywhere in the document" would be checking the wrong
    # thing entirely.
    assert b"<base" not in unframed.split(b"<script")[0]
    body = b"<html>page</html>"
    assert body in framed and body in unframed
    assert framed.replace(b'<base href="about:srcdoc">\n', b"") == unframed

    pillar = db.create_pillar(mission_id, "P", "sess", "#4ade80")
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>pillar</html>", "first")
    over_channel = asyncio.run(mc_api.handle_relay_read(
        "guest:1", mission_id,
        {"kind": "pillar_site", "pillar_id": pillar["pillar_id"]},
    ))["document"]
    assert over_channel == compose.compose_screen(
        mission_id, pillar["pillar_id"], framed=True,
        may_write=True, viewer="guest:1",
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
    document = compose.compose_screen(
        mission["mission_id"], framed=True).decode("utf-8")
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


def test_a_link_with_no_identity_is_told_it_cannot_write():
    """A grant with no bound participant has its writes refused by
    _serve_write -- correctly, since an attributed record has nobody to
    attribute to. The DOCUMENT is told, so a control that cannot work is
    disabled with a reason rather than offered and failing on tap."""
    mission = db.create_mission("Read Only")
    mission_id = mission["mission_id"]
    db.push_site_revision(mission_id, "<html>page</html>", "first")

    anonymous = link_serving._resolve_mission(mission_id, {"meta": {}})["viewer"]
    assert b'"may_write":false' in anonymous.replace(b" ", b"")

    named = link_serving._resolve_mission(
        mission_id, {"meta": {"participant_id": "guest:jeremy"}})["viewer"]
    assert b'"may_write":true' in named.replace(b" ", b"")


def test_the_no_channel_timer_is_armed_only_inside_a_frame():
    """At a real URL no MessagePort is ever transferred and none is wanted —
    the transport is HTTP. Arming the port timeout unconditionally made the
    dashboard declare "Not connected. Posting is disabled." six seconds after
    every load, hide the composer and flag the bar "no link", while the HTTP
    path underneath worked perfectly.

    Asserted on the source because the defect is a MISSING GUARD, not a value:
    the symptom only appears after a six-second timer in a real browser, and a
    test that waits for it would be slower and no more truthful.
    """
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()
    marker = "if (!port) { ui.noChannel = true; render(); }"
    assert marker in src, "the port-timeout guard moved; re-read this test"

    before = src[: src.index(marker)]
    guard = before.rindex("window.parent !== window")
    timer = before.rindex("setTimeout")
    assert guard < timer, (
        "the noChannel timer must sit INSIDE a `window.parent !== window` "
        "guard; unguarded it disables posting on every top-level surface"
    )


# ── the status feed (pillar status posts kept, not replaced) ──────


def _mission_with_pillar(tmp_path):
    from tools.dashboard.dao import mission_control_db as mdb

    path = tmp_path / "mc.db"
    mdb.init_db(path)
    mission_id = mdb.create_mission("M", "sess", db_path=path)["mission_id"]
    pillar_id = mdb.create_pillar(
        mission_id, "Infra", coordinator_session="s", color="#34d399", db_path=path
    )["pillar_id"]
    return path, mission_id, pillar_id


def test_status_lines_accumulate_instead_of_overwriting(tmp_path):
    """The line was already being written to a standard, on a cadence, by
    every pillar — and every one but the latest was discarded. Keeping them
    is the difference between knowing what is true now and being able to see
    what has been happening.
    """
    from tools.dashboard.dao import mission_control_db as mdb

    path, mission_id, pillar_id = _mission_with_pillar(tmp_path)
    mdb.set_pillar_last_done(pillar_id, "First thing finished.", db_path=path)
    mdb.set_pillar_last_done(pillar_id, "Second thing finished.", db_path=path)

    feed = mdb.list_mission_status_posts(mission_id, db_path=path)
    assert [p["text"] for p in feed] == [
        "Second thing finished.", "First thing finished.",
    ], "newest first, and nothing discarded"
    assert feed[0]["pillar_name"] == "Infra", "a merged feed must say who wrote each line"


def test_clearing_the_line_posts_nothing(tmp_path):
    """Clearing is not an event. It resets the card to 'never written'; it is
    not a thing that happened and must not appear in the account of what did.
    """
    from tools.dashboard.dao import mission_control_db as mdb

    path, mission_id, pillar_id = _mission_with_pillar(tmp_path)
    mdb.set_pillar_last_done(pillar_id, "Something finished.", db_path=path)
    mdb.set_pillar_last_done(pillar_id, "", db_path=path)

    assert len(mdb.list_mission_status_posts(mission_id, db_path=path)) == 1


def test_a_write_against_an_unknown_pillar_leaves_no_orphan(tmp_path):
    """The post is only true if the pillar it names exists."""
    from tools.dashboard.dao import mission_control_db as mdb

    path, mission_id, _ = _mission_with_pillar(tmp_path)
    assert mdb.set_pillar_last_done("no-such-pillar", "orphan", db_path=path) is False
    assert mdb.list_mission_status_posts(mission_id, db_path=path) == []


def test_age_follows_the_status_line_when_it_is_newer_than_the_push(tmp_path):
    """A pillar reporting steadily for an hour used to read as untouched
    since its last revision — the opposite of the truth, on the one field a
    reader glances at to decide whether anything is happening.
    """
    from tools.dashboard.plugins.mission_control import compose

    older_push = {"created_at": 1_000.0}
    assert compose._latest_sign_of_life(older_push, {"last_done_at": 2_000.0}) == 2_000.0
    assert compose._latest_sign_of_life(older_push, {"last_done_at": None}) == 1_000.0
    assert compose._latest_sign_of_life(None, {"last_done_at": 2_000.0}) == 2_000.0
    assert compose._latest_sign_of_life(None, {}) is None


def test_presence_rows_carry_the_session_name_a_link_needs():
    """An agent in the presence list is a session, and its participant id is
    that session's name — so the row can open the session viewer. The link is
    only buildable if the id survives into the screen state; without it the
    viewer would have a kind and a label and nothing to navigate to.
    """
    from tools.dashboard.plugins.mission_control import compose

    src = compose._presence.__doc__ or ""
    assert src is not None
    import inspect
    body = inspect.getsource(compose._presence)
    assert '"participant_id"' in body, (
        "presence rows must carry participant_id; an agent row's id is its "
        "session name and is the only thing a session link can be built from"
    )
    assert '"kind"' in body, (
        "presence rows must carry the kind; a person is not a session and "
        "must not be rendered as a link to one"
    )


def test_live_updates_are_subscribed_only_at_a_real_url():
    """A framed screen is fed events by its host over the channel; a screen at
    a real URL had no source at all and showed page-load state forever.

    Asserted on the built source because the defect is a MISSING CALL and a
    MISSING GUARD — both invisible to any test that only renders the page.
    """
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()

    # Match the CALL, not the definition. "subscribeLive()" alone also
    # matches "function subscribeLive()", so the first version of this
    # assertion passed with the call deleted — it could not fail for the
    # reason it exists.
    calls = [ln for ln in src.splitlines()
             if "subscribeLive()" in ln and "function" not in ln]
    assert calls, "subscribeLive is defined but never called"

    body = src[src.index("function subscribeLive"):]
    body = body[: body.index("// ---- chrome")]
    guard = body.index("window.parent !== window")
    opened = body.index("new EventSource")
    assert guard < opened, (
        "EventSource must be constructed only at top level; a framed screen "
        "is fed by its host and must not open a second source"
    )
    assert "mission_control:conversation" in body
    assert "data.mission_id !== state.mission_id" in body, (
        "events for another mission must be dropped"
    )


def test_a_live_render_preserves_what_is_being_typed():
    """render() clears and rebuilds the whole chrome. Once events can trigger
    it, an answer half-written when someone else posts would be destroyed —
    worse than the stale screen live updates exist to fix.

    root.activeElement, not document.activeElement: the chrome lives in a
    CLOSED shadow root, so the document reports the host element instead of
    the field inside it, and the value would never be captured.
    """
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()
    body = src[src.index("function render()"):]
    body = body[: body.index("// ---- anchored controls")]
    # Comments explain WHY document.activeElement is wrong here, so the
    # assertion has to read code rather than prose.
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("//")
    )

    assert "root.activeElement" in code
    assert "document.activeElement" not in code, (
        "a closed shadow root reports its host, not the focused field"
    )
    assert "setSelectionRange" in code, "the caret position is part of the text"
    assert code.index("root.activeElement") < code.index('chrome.textContent = ""'), (
        "what is typed must be captured BEFORE the chrome is torn down"
    )


def test_anchor_counts_are_repainted_and_not_frozen_at_mount():
    """mountAnchors only ADDS controls — it returns early on any element that
    already carries one. So a control mounted before a question was asked kept
    the count it was born with, while the top bar recounted on every render.
    The anchor was frozen, not miscounted.

    The closed shadow root is why this needs a kept reference: it cannot be
    reached again from the holder element, so a control not recorded at mount
    can never be updated.
    """
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()
    assert "anchorControls" in src, "mounted controls must be kept to be repainted"
    calls = [ln for ln in src.splitlines()
             if "repaintAnchors()" in ln and "function" not in ln]
    assert calls, "repaintAnchors is defined but never called"

    render = src[src.index("function render()"):]
    render = render[: render.index("// ---- anchored controls")]
    assert "repaintAnchors()" in render, (
        "counts must be refreshed wherever the question list is re-read"
    )


def test_an_anchor_shows_unanswered_and_answered_separately():
    """One total cannot tell 'three still waiting on me' from 'three already
    settled', which is the whole reason to look at an anchor. Neither count is
    drawn at zero: an anchor nobody has asked about stays a bare bubble.
    """
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()
    paint = src[src.index("function paintAnchor"):]
    paint = paint[: paint.index("function repaintAnchors")]

    assert "mc-count-open" in paint and "mc-count-done" in paint, (
        "an anchor shows unanswered and answered as separate counts"
    )
    assert "if (open)" in paint and "if (done)" in paint, (
        "a zero count is not drawn"
    )


def test_a_replayed_status_event_does_not_duplicate_the_entry():
    """Subscribing replays the cached state of every topic, and EventSource
    reconnects on its own, so the same status arrives again on every
    reconnect — and once at first connect on top of the copy already inlined
    in the page. Three reconnects showed three identical entries in the feed.

    The conversation handler was never affected because it finds an entry by
    id and REPLACES it. Only the activity handler appended.
    """
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()
    body = src[src.index("function applyActivity"):]
    body = body[: body.index("// ---- live updates")]

    assert "already" in body and "unshift" in body
    assert body.index("already") < body.index("unshift"), (
        "the duplicate check must run before the entry is added"
    )
    assert "x.text === data.text" in body, (
        "identical text from the same pillar is the same report, not news"
    )


def test_opening_a_question_records_where_it_was_opened_from():
    """Closing a discussion could only mean 'close everything', so reading one
    question and wanting the next meant reopening the list by hand.
    """
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()
    assert 'from: {panel: "questions"}' in src, (
        "a question opened from the list must remember the list"
    )
    assert "function goBack" in src and "show(ui.from || null)" in src, (
        "back goes one step, falling through to close only when there is "
        "nowhere to return to"
    )


def test_the_question_list_can_be_filtered():
    from tools.dashboard.scripts import build_mission_viewer as builder

    src = builder.bootstrap_source()
    assert "var qFilter" in src
    rows = src[src.index("function questionRows"):]
    rows = rows[:800]
    assert 'qFilter === "open"' in rows and 'qFilter === "done"' in rows, (
        "the filter must actually be applied to the rows, not only rendered"
    )
