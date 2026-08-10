from __future__ import annotations

import pytest

from tools.dashboard.dao import mission_control_db as db


def _db_path(tmp_path):
    return tmp_path / "mission_control.db"


def test_create_and_get_mission(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", "auto-coordinator", db_path=path)
    assert mission["name"] == "OSS Insights"
    assert mission["coordinator_session"] == "auto-coordinator"
    assert mission["current_revision_id"] is None
    assert mission["status"] == "active"

    fetched = db.get_mission(mission["mission_id"], db_path=path)
    assert fetched["mission_id"] == mission["mission_id"]
    assert fetched["name"] == "OSS Insights"
    assert fetched["status"] == "active"


def test_set_mission_status_updates_and_persists(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.set_mission_status(mission["mission_id"], "paused", db_path=path) is True
    assert db.get_mission(mission["mission_id"], db_path=path)["status"] == "paused"


def test_set_mission_status_missing_mission_returns_false(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.set_mission_status("nope", "paused", db_path=path) is False


def test_set_mission_status_rejects_invalid_status(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    with pytest.raises(AssertionError):
        db.set_mission_status(mission["mission_id"], "nope", db_path=path)


def test_get_mission_missing_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.get_mission("does-not-exist", db_path=path) is None


def test_status_column_migrates_onto_a_pre_existing_database(tmp_path):
    """A missions table created before the status column existed (no
    ALTER TABLE has ever run against it) must gain the column -- with the
    documented default -- the next time anything opens the DB, not error
    or silently omit status from old rows."""
    import sqlite3

    path = _db_path(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE missions ("
        " mission_id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " coordinator_session TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, current_revision_id TEXT)"
    )
    conn.execute(
        "INSERT INTO missions (mission_id, name, coordinator_session, created_at)"
        " VALUES ('legacy-1', 'Pre-status mission', '', 0)",
    )
    conn.commit()
    conn.close()

    fetched = db.get_mission("legacy-1", db_path=path)
    assert fetched["status"] == "active"


def test_read_paths_do_not_require_init_db_first(tmp_path):
    """A fresh deployment's first-ever request can be a read (list/get).

    Regression: only the write paths used to call the schema-ensure
    helper, so a read against a DB file that had never been written to
    raised sqlite3.OperationalError('no such table') instead of just
    returning empty/None. Every DAO call must tolerate a db_path whose
    file (and therefore table) doesn't exist yet — no db.init_db() call
    here, deliberately.
    """
    path = _db_path(tmp_path)
    assert not path.exists()
    assert db.list_missions(db_path=path) == []
    assert db.get_mission("nope", db_path=path) is None
    assert db.get_current_site("nope", db_path=path) is None
    assert db.list_site_revisions("nope", db_path=path) == []
    assert db.get_site_revision("nope", "nope", db_path=path) is None
    assert db.delete_mission("nope", db_path=path) is False
    assert db.activate_site_revision("nope", "nope", db_path=path) is False
    assert db.get_last_seen("nope", db_path=path) is None
    assert db.list_site_revisions_since("nope", 0, db_path=path) == []
    assert db.list_conversation_since("nope", 0, db_path=path) == []


def test_list_missions_orders_newest_first(tmp_path):
    path = _db_path(tmp_path)
    first = db.create_mission("First", db_path=path)
    second = db.create_mission("Second", db_path=path)
    listed = db.list_missions(db_path=path)
    assert [m["mission_id"] for m in listed] == [second["mission_id"], first["mission_id"]]


def test_delete_mission_removes_mission_and_revisions(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("Doomed", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v1</html>", db_path=path)

    assert db.delete_mission(mission["mission_id"], db_path=path) is True
    assert db.get_mission(mission["mission_id"], db_path=path) is None
    assert db.list_site_revisions(mission["mission_id"], db_path=path) == []


def test_delete_mission_removes_conversation_and_last_seen(tmp_path):
    """Pre-existing gap fixed alongside the last-seen watermark: deleting
    a mission left mission_conversation rows and the watermark orphaned,
    keyed off a mission_id nothing else references anymore."""
    path = _db_path(tmp_path)
    mission = db.create_mission("Doomed", db_path=path)
    db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.mark_seen(mission["mission_id"], db_path=path)

    assert db.delete_mission(mission["mission_id"], db_path=path) is True
    assert db.list_conversation(mission["mission_id"], db_path=path) == []
    assert db.get_last_seen(mission["mission_id"], db_path=path) is None


def test_delete_missing_mission_returns_false(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.delete_mission("nope", db_path=path) is False


def test_push_site_revision_stores_and_publishes_in_one_call(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)

    revision = db.push_site_revision(
        mission["mission_id"], "<html>v1</html>", "first push", db_path=path,
    )
    assert revision["revision_seq"] == 1
    assert revision["note"] == "first push"

    current = db.get_current_site(mission["mission_id"], db_path=path)
    assert current["revision_id"] == revision["revision_id"]
    assert current["html"] == "<html>v1</html>"


def test_push_site_revision_against_missing_mission_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.push_site_revision("nope", "<html></html>", db_path=path) is None


def test_push_site_revision_increments_seq_and_updates_current(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    rev1 = db.push_site_revision(mission["mission_id"], "<html>v1</html>", db_path=path)
    rev2 = db.push_site_revision(mission["mission_id"], "<html>v2</html>", db_path=path)

    assert rev1["revision_seq"] == 1
    assert rev2["revision_seq"] == 2

    current = db.get_current_site(mission["mission_id"], db_path=path)
    assert current["revision_id"] == rev2["revision_id"]
    assert current["html"] == "<html>v2</html>"


def test_get_current_site_none_before_any_push(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.get_current_site(mission["mission_id"], db_path=path) is None


def test_list_site_revisions_newest_first_without_html(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v1</html>", "note1", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v2</html>", "note2", db_path=path)

    revisions = db.list_site_revisions(mission["mission_id"], db_path=path)
    assert [r["revision_seq"] for r in revisions] == [2, 1]
    assert "html" not in revisions[0]
    assert revisions[0]["byte_size"] == len("<html>v2</html>")


def test_get_site_revision_returns_immutable_historical_content(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    rev1 = db.push_site_revision(mission["mission_id"], "<html>v1</html>", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v2</html>", db_path=path)

    fetched = db.get_site_revision(mission["mission_id"], rev1["revision_id"], db_path=path)
    assert fetched["html"] == "<html>v1</html>"
    assert fetched["revision_seq"] == 1


def test_get_site_revision_missing_returns_none(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.get_site_revision(mission["mission_id"], "nope", db_path=path) is None


def test_activate_site_revision_rolls_back_current_pointer_without_new_revision(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    rev1 = db.push_site_revision(mission["mission_id"], "<html>good</html>", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>bad</html>", db_path=path)

    ok = db.activate_site_revision(mission["mission_id"], rev1["revision_id"], db_path=path)
    assert ok is True

    current = db.get_current_site(mission["mission_id"], db_path=path)
    assert current["revision_id"] == rev1["revision_id"]
    assert current["html"] == "<html>good</html>"
    # Rolling back does not fabricate a new revision — history stays honest.
    assert len(db.list_site_revisions(mission["mission_id"], db_path=path)) == 2


def test_activate_site_revision_missing_revision_returns_false(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v1</html>", db_path=path)
    assert db.activate_site_revision(mission["mission_id"], "nope", db_path=path) is False


def test_activate_site_revision_wrong_mission_returns_false(tmp_path):
    path = _db_path(tmp_path)
    mission_a = db.create_mission("A", db_path=path)
    mission_b = db.create_mission("B", db_path=path)
    rev_a = db.push_site_revision(mission_a["mission_id"], "<html>a</html>", db_path=path)

    assert db.activate_site_revision(mission_b["mission_id"], rev_a["revision_id"], db_path=path) is False


# ── Visitor identity shim (P2) ────────────────────────────────────


def test_create_visitor_token_returns_token_and_stable_participant_id(tmp_path):
    path = _db_path(tmp_path)
    visitor = db.create_visitor_token("Alex", db_path=path)
    assert visitor["token"]
    assert visitor["participant_id"].startswith("guest:")
    assert visitor["display_name"] == "Alex"


def test_create_visitor_token_participant_id_independent_of_token(tmp_path):
    path = _db_path(tmp_path)
    visitor = db.create_visitor_token("Alex", db_path=path)
    assert visitor["participant_id"] != visitor["token"]


def test_resolve_visitor_returns_participant_id_and_label(tmp_path):
    path = _db_path(tmp_path)
    visitor = db.create_visitor_token("Alex", db_path=path)
    resolved = db.resolve_visitor(visitor["token"], db_path=path)
    assert resolved == {
        "participant_id": visitor["participant_id"],
        "participant_label": "Alex",
    }


def test_resolve_visitor_unknown_token_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.resolve_visitor("nope", db_path=path) is None


def test_resolve_visitor_never_leaks_the_token_itself(tmp_path):
    """The token is the bearer secret; resolve_visitor's return shape
    must never include it, only the display-safe participant_id/label."""
    path = _db_path(tmp_path)
    visitor = db.create_visitor_token("Alex", db_path=path)
    resolved = db.resolve_visitor(visitor["token"], db_path=path)
    assert "token" not in resolved


def test_get_visitor_by_participant_id_returns_display_name(tmp_path):
    path = _db_path(tmp_path)
    visitor = db.create_visitor_token("Priya (data partner)", db_path=path)
    found = db.get_visitor_by_participant_id(visitor["participant_id"], db_path=path)
    assert found == {
        "participant_id": visitor["participant_id"],
        "display_name": "Priya (data partner)",
    }


def test_get_visitor_by_participant_id_unknown_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.get_visitor_by_participant_id("guest:nope", db_path=path) is None


def test_get_visitor_by_participant_id_never_takes_or_returns_a_token(tmp_path):
    """The lookup direction this function serves (a caller already holding
    a participant_id, confirming it's real before binding a grant to it)
    must never accept or leak the bearer token -- participant_id only."""
    path = _db_path(tmp_path)
    visitor = db.create_visitor_token("Alex", db_path=path)
    found = db.get_visitor_by_participant_id(visitor["participant_id"], db_path=path)
    assert "token" not in found
    assert db.get_visitor_by_participant_id(visitor["token"], db_path=path) is None


# ── Mission conversation (P2 Q&A) ─────────────────────────────────


def test_ask_question_returns_pending_entry(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(
        mission["mission_id"], "What's the timeline?", "guest:abc", "Alex", db_path=path,
    )
    assert entry["question"] == "What's the timeline?"
    assert entry["asked_by_participant_id"] == "guest:abc"
    assert entry["asked_by_label"] == "Alex"
    assert entry["answer"] is None
    assert entry["relay_status"] == "pending"


def test_ask_question_missing_mission_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.ask_question("nope", "q", "guest:abc", "Alex", db_path=path) is None


def test_ask_question_label_is_a_snapshot_not_a_live_join(tmp_path):
    """If a visitor's display name is later reissued under the same
    participant_id (or the token record otherwise changes), a past
    question keeps showing what they were called when they asked."""
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(
        mission["mission_id"], "q1", "guest:abc", "Alex", db_path=path,
    )
    # No live join exists in this schema -- list_conversation must
    # continue returning the original snapshot regardless of anything
    # else happening to visitor_tokens.
    listed = db.list_conversation(mission["mission_id"], db_path=path)
    assert listed[0]["asked_by_label"] == "Alex"
    assert listed[0]["entry_id"] == entry["entry_id"]


def test_get_question_returns_none_when_missing(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.get_question(mission["mission_id"], "nope", db_path=path) is None


def test_list_conversation_orders_oldest_first(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    e1 = db.ask_question(mission["mission_id"], "q1", "guest:a", "A", db_path=path)
    e2 = db.ask_question(mission["mission_id"], "q2", "guest:b", "B", db_path=path)
    listed = db.list_conversation(mission["mission_id"], db_path=path)
    assert [e["entry_id"] for e in listed] == [e1["entry_id"], e2["entry_id"]]


def test_count_open_questions(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.count_open_questions(mission["mission_id"], db_path=path) == 0

    e1 = db.ask_question(mission["mission_id"], "q1", "guest:a", "A", db_path=path)
    db.ask_question(mission["mission_id"], "q2", "guest:b", "B", db_path=path)
    assert db.count_open_questions(mission["mission_id"], db_path=path) == 2

    db.answer_question(mission["mission_id"], e1["entry_id"], "ans", "auto-x", db_path=path)
    assert db.count_open_questions(mission["mission_id"], db_path=path) == 1


def test_count_open_questions_no_mission_returns_zero(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.count_open_questions("nope", db_path=path) == 0


def test_mark_question_relay_status_updates_status(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.mark_question_relay_status(mission["mission_id"], entry["entry_id"], "sent", db_path=path)
    fetched = db.get_question(mission["mission_id"], entry["entry_id"], db_path=path)
    assert fetched["relay_status"] == "sent"


def test_mark_question_relay_status_rejects_invalid_status(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    with pytest.raises(AssertionError):
        db.mark_question_relay_status(mission["mission_id"], entry["entry_id"], "nope", db_path=path)


def test_answer_question_records_answer_and_answerer_snapshot(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", "auto-original-coordinator", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)

    answered = db.answer_question(
        mission["mission_id"], entry["entry_id"], "Q3 2026",
        "auto-actual-answerer", db_path=path,
    )
    assert answered["answer"] == "Q3 2026"
    assert answered["answered_by_session"] == "auto-actual-answerer"
    assert answered["answered_at"] is not None


def test_answer_question_missing_entry_returns_none(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.answer_question(
        mission["mission_id"], "nope", "answer", "auto-x", db_path=path,
    ) is None


def test_answer_question_records_answerer_even_if_different_from_current_coordinator(tmp_path):
    """coordinator_session on the mission row is mutable by design --
    the answer must record who actually answered, not what the mission
    row says now or later."""
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", "auto-coordinator-v1", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)

    answered = db.answer_question(
        mission["mission_id"], entry["entry_id"], "answer",
        "auto-coordinator-v1", db_path=path,
    )
    assert answered["answered_by_session"] == "auto-coordinator-v1"

    # Mission changes hands -- a NEW answer records the NEW answerer,
    # the old answer's snapshot is untouched.
    conn = db._get_conn(path)
    conn.execute(
        "UPDATE missions SET coordinator_session = ? WHERE mission_id = ?",
        ("auto-coordinator-v2", mission["mission_id"]),
    )
    conn.commit()
    conn.close()

    entry2 = db.ask_question(mission["mission_id"], "q2", "guest:b", "B", db_path=path)
    answered2 = db.answer_question(
        mission["mission_id"], entry2["entry_id"], "answer2",
        "auto-coordinator-v2", db_path=path,
    )
    assert answered2["answered_by_session"] == "auto-coordinator-v2"
    # First answer's snapshot is unaffected by the mission's coordinator changing.
    first_still = db.get_question(mission["mission_id"], entry["entry_id"], db_path=path)
    assert first_still["answered_by_session"] == "auto-coordinator-v1"


# ── "Since last visit" watermark (P3) ─────────────────────────────


def test_get_last_seen_none_before_any_mark(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.get_last_seen(mission["mission_id"], db_path=path) is None


def test_mark_seen_persists_watermark(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    seen_at = db.mark_seen(mission["mission_id"], db_path=path)
    assert db.get_last_seen(mission["mission_id"], db_path=path) == seen_at


def test_mark_seen_upserts_on_repeated_calls(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    first = db.mark_seen(mission["mission_id"], db_path=path)
    second = db.mark_seen(mission["mission_id"], db_path=path)
    assert second >= first
    assert db.get_last_seen(mission["mission_id"], db_path=path) == second


def test_list_site_revisions_since_only_returns_newer(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v1</html>", "old", db_path=path)
    watermark = db.mark_seen(mission["mission_id"], db_path=path)
    rev2 = db.push_site_revision(mission["mission_id"], "<html>v2</html>", "new", db_path=path)

    since = db.list_site_revisions_since(mission["mission_id"], watermark, db_path=path)
    assert [r["revision_id"] for r in since] == [rev2["revision_id"]]
    assert "html" not in since[0]


def test_list_conversation_since_only_returns_newer(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    db.ask_question(mission["mission_id"], "old", "guest:a", "A", db_path=path)
    watermark = db.mark_seen(mission["mission_id"], db_path=path)
    new_entry = db.ask_question(mission["mission_id"], "new", "guest:b", "B", db_path=path)

    since = db.list_conversation_since(mission["mission_id"], watermark, db_path=path)
    assert [e["entry_id"] for e in since] == [new_entry["entry_id"]]


def test_list_since_helpers_before_any_watermark_use_epoch_zero(tmp_path):
    """A caller passing 0 (e.g. a mission that's never been marked seen,
    if the API chose to treat that as 'everything is new') gets the full
    history back -- the DAO itself doesn't special-case an absent
    watermark, that policy choice lives in the API layer."""
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v1</html>", db_path=path)
    assert len(db.list_site_revisions_since(mission["mission_id"], 0, db_path=path)) == 1


# ── Pillars (P4) ───────────────────────────────────────────────────


def test_create_and_get_pillar(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(
        mission["mission_id"], "Dataset & Schema", "auto-schema", "#34d399", db_path=path,
    )
    assert pillar["mission_id"] == mission["mission_id"]
    assert pillar["name"] == "Dataset & Schema"
    assert pillar["coordinator_session"] == "auto-schema"
    assert pillar["color"] == "#34d399"
    assert pillar["current_revision_id"] is None
    assert pillar["status"] == "active"

    fetched = db.get_pillar(pillar["pillar_id"], db_path=path)
    assert fetched["pillar_id"] == pillar["pillar_id"]
    assert fetched["name"] == "Dataset & Schema"


def test_get_pillar_missing_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.get_pillar("nope", db_path=path) is None


def test_list_pillars_orders_oldest_first_and_scopes_to_mission(tmp_path):
    path = _db_path(tmp_path)
    m1 = db.create_mission("A", db_path=path)
    m2 = db.create_mission("B", db_path=path)
    p1 = db.create_pillar(m1["mission_id"], "First", db_path=path)
    p2 = db.create_pillar(m1["mission_id"], "Second", db_path=path)
    db.create_pillar(m2["mission_id"], "Other mission's pillar", db_path=path)

    listed = db.list_pillars(m1["mission_id"], db_path=path)
    assert [p["pillar_id"] for p in listed] == [p1["pillar_id"], p2["pillar_id"]]


def test_set_pillar_status_updates_and_persists(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    assert db.set_pillar_status(pillar["pillar_id"], "paused", db_path=path) is True
    assert db.get_pillar(pillar["pillar_id"], db_path=path)["status"] == "paused"


def test_set_pillar_status_missing_pillar_returns_false(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.set_pillar_status("nope", "paused", db_path=path) is False


def test_set_pillar_status_rejects_invalid_status(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    with pytest.raises(AssertionError):
        db.set_pillar_status(pillar["pillar_id"], "nope", db_path=path)


def test_delete_pillar_removes_pillar_and_revisions(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>v1</html>", db_path=path)

    assert db.delete_pillar(pillar["pillar_id"], db_path=path) is True
    assert db.get_pillar(pillar["pillar_id"], db_path=path) is None
    assert db.list_pillar_site_revisions(pillar["pillar_id"], db_path=path) == []


def test_delete_missing_pillar_returns_false(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.delete_pillar("nope", db_path=path) is False


def test_delete_mission_cascades_to_its_pillars(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>v1</html>", db_path=path)
    db.mark_pillar_seen(pillar["pillar_id"], db_path=path)

    assert db.delete_mission(mission["mission_id"], db_path=path) is True
    assert db.get_pillar(pillar["pillar_id"], db_path=path) is None
    assert db.list_pillar_site_revisions(pillar["pillar_id"], db_path=path) == []
    assert db.get_pillar_last_seen(pillar["pillar_id"], db_path=path) is None


def test_delete_mission_does_not_touch_other_missions_pillars(tmp_path):
    path = _db_path(tmp_path)
    m1 = db.create_mission("A", db_path=path)
    m2 = db.create_mission("B", db_path=path)
    p2 = db.create_pillar(m2["mission_id"], "P2", db_path=path)

    db.delete_mission(m1["mission_id"], db_path=path)
    assert db.get_pillar(p2["pillar_id"], db_path=path) is not None


def test_push_pillar_site_revision_stores_and_publishes(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)

    revision = db.push_pillar_site_revision(
        pillar["pillar_id"], "<html>v1</html>", "first push", db_path=path,
    )
    assert revision["revision_seq"] == 1
    assert revision["note"] == "first push"

    current = db.get_current_pillar_site(pillar["pillar_id"], db_path=path)
    assert current["revision_id"] == revision["revision_id"]
    assert current["html"] == "<html>v1</html>"


def test_push_pillar_site_revision_missing_pillar_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.push_pillar_site_revision("nope", "<html></html>", db_path=path) is None


def test_push_pillar_site_revision_increments_seq(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    rev1 = db.push_pillar_site_revision(pillar["pillar_id"], "<html>v1</html>", db_path=path)
    rev2 = db.push_pillar_site_revision(pillar["pillar_id"], "<html>v2</html>", db_path=path)
    assert rev1["revision_seq"] == 1
    assert rev2["revision_seq"] == 2


def test_activate_pillar_site_revision_rolls_back_without_new_revision(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    rev1 = db.push_pillar_site_revision(pillar["pillar_id"], "<html>good</html>", db_path=path)
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>bad</html>", db_path=path)

    ok = db.activate_pillar_site_revision(pillar["pillar_id"], rev1["revision_id"], db_path=path)
    assert ok is True
    current = db.get_current_pillar_site(pillar["pillar_id"], db_path=path)
    assert current["html"] == "<html>good</html>"
    assert len(db.list_pillar_site_revisions(pillar["pillar_id"], db_path=path)) == 2


def test_pillar_last_seen_watermark(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    assert db.get_pillar_last_seen(pillar["pillar_id"], db_path=path) is None
    seen_at = db.mark_pillar_seen(pillar["pillar_id"], db_path=path)
    assert db.get_pillar_last_seen(pillar["pillar_id"], db_path=path) == seen_at


def test_list_pillar_site_revisions_since_only_returns_newer(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>v1</html>", "old", db_path=path)
    watermark = db.mark_pillar_seen(pillar["pillar_id"], db_path=path)
    rev2 = db.push_pillar_site_revision(pillar["pillar_id"], "<html>v2</html>", "new", db_path=path)

    since = db.list_pillar_site_revisions_since(pillar["pillar_id"], watermark, db_path=path)
    assert [r["revision_id"] for r in since] == [rev2["revision_id"]]


# ── Anchored conversation (mission-level vs pillar-level) ──────────


def test_ask_question_mission_level_unaffected_by_pillars(tmp_path):
    """Backward compatibility: a mission-level ask_question call (no
    pillar_id) behaves exactly as it did before pillars existed."""
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    assert entry["pillar_id"] is None
    assert entry["anchor"] is None


def test_ask_question_pillar_scoped_requires_pillar_to_exist(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.ask_question(
        mission["mission_id"], "q", "guest:a", "A", pillar_id="nope", db_path=path,
    ) is None


def test_ask_question_pillar_scoped_stores_pillar_and_anchor(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    entry = db.ask_question(
        mission["mission_id"], "q", "guest:a", "A",
        pillar_id=pillar["pillar_id"], anchor="table:x", db_path=path,
    )
    assert entry["mission_id"] == mission["mission_id"]
    assert entry["pillar_id"] == pillar["pillar_id"]
    assert entry["anchor"] == "table:x"


def test_list_conversation_excludes_pillar_scoped_entries(tmp_path):
    """Mission-level list_conversation only ever returns pillar_id IS NULL
    rows -- pillar activity doesn't leak into the mission's own Q&A list."""
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    mission_entry = db.ask_question(mission["mission_id"], "mission q", "guest:a", "A", db_path=path)
    db.ask_question(
        mission["mission_id"], "pillar q", "guest:b", "B",
        pillar_id=pillar["pillar_id"], db_path=path,
    )

    listed = db.list_conversation(mission["mission_id"], db_path=path)
    assert [e["entry_id"] for e in listed] == [mission_entry["entry_id"]]


def test_list_pillar_conversation_returns_only_that_pillars_entries(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    p1 = db.create_pillar(mission["mission_id"], "P1", db_path=path)
    p2 = db.create_pillar(mission["mission_id"], "P2", db_path=path)
    e1 = db.ask_question(mission["mission_id"], "q1", "guest:a", "A", pillar_id=p1["pillar_id"], db_path=path)
    db.ask_question(mission["mission_id"], "q2", "guest:b", "B", pillar_id=p2["pillar_id"], db_path=path)

    listed = db.list_pillar_conversation(p1["pillar_id"], db_path=path)
    assert [e["entry_id"] for e in listed] == [e1["entry_id"]]


def test_count_open_questions_excludes_pillar_scoped(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    db.ask_question(mission["mission_id"], "mission q", "guest:a", "A", db_path=path)
    db.ask_question(mission["mission_id"], "pillar q", "guest:b", "B", pillar_id=pillar["pillar_id"], db_path=path)

    assert db.count_open_questions(mission["mission_id"], db_path=path) == 1
    assert db.count_open_pillar_questions(pillar["pillar_id"], db_path=path) == 1


def test_list_conversation_since_excludes_pillar_scoped(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    watermark = db.mark_seen(mission["mission_id"], db_path=path)
    db.ask_question(mission["mission_id"], "mission q", "guest:a", "A", db_path=path)
    db.ask_question(mission["mission_id"], "pillar q", "guest:b", "B", pillar_id=pillar["pillar_id"], db_path=path)

    since = db.list_conversation_since(mission["mission_id"], watermark, db_path=path)
    assert [e["question"] for e in since] == ["mission q"]


def test_list_pillar_conversation_since_only_returns_newer(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    db.ask_question(mission["mission_id"], "old", "guest:a", "A", pillar_id=pillar["pillar_id"], db_path=path)
    watermark = db.mark_pillar_seen(pillar["pillar_id"], db_path=path)
    new_entry = db.ask_question(
        mission["mission_id"], "new", "guest:b", "B", pillar_id=pillar["pillar_id"], db_path=path,
    )

    since = db.list_pillar_conversation_since(pillar["pillar_id"], watermark, db_path=path)
    assert [e["entry_id"] for e in since] == [new_entry["entry_id"]]


def test_conversation_migrates_onto_pre_existing_database(tmp_path):
    """A mission_conversation table created before pillar_id/anchor
    existed must gain both columns -- NULL on old rows -- the next time
    anything opens the DB, matching the status-column migration test's
    shape."""
    import sqlite3

    path = _db_path(tmp_path)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE mission_conversation ("
        " entry_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, question TEXT NOT NULL,"
        " asked_by_participant_id TEXT NOT NULL, asked_by_label TEXT NOT NULL,"
        " answer TEXT, answered_by_session TEXT, answered_at REAL,"
        " relay_status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO mission_conversation (entry_id, mission_id, question,"
        " asked_by_participant_id, asked_by_label, relay_status, created_at)"
        " VALUES ('legacy-1', 'm1', 'old question', 'guest:a', 'A', 'pending', 0)",
    )
    conn.commit()
    conn.close()

    fetched = db.get_question("m1", "legacy-1", db_path=path)
    assert fetched["pillar_id"] is None
    assert fetched["anchor"] is None


# ── Progress updates ─────────────────────────────────────────────


def test_add_and_list_conversation_updates(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)

    db.add_conversation_update(entry["entry_id"], "still working", db_path=path)
    db.add_conversation_update(entry["entry_id"], "almost done", db_path=path)

    updates = db.list_conversation_updates(entry["entry_id"], db_path=path)
    assert [u["text"] for u in updates] == ["still working", "almost done"]


def test_conversation_update_never_touches_answer(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.add_conversation_update(entry["entry_id"], "still working", db_path=path)

    fetched = db.get_question(mission["mission_id"], entry["entry_id"], db_path=path)
    assert fetched["answer"] is None


def test_list_conversation_updates_empty_for_unknown_entry(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.list_conversation_updates("nope", db_path=path) == []


# ── Reopening ─────────────────────────────────────────────────────


def test_reopen_question_clears_answer_and_returns_to_open(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.answer_question(mission["mission_id"], entry["entry_id"], "first answer", "auto-x", db_path=path)

    reopened = db.reopen_question(
        mission["mission_id"], entry["entry_id"], "not quite -- what about X?", "A", db_path=path,
    )

    assert reopened["answer"] is None
    assert reopened["answered_by_session"] is None
    assert reopened["answered_at"] is None
    assert reopened["relay_status"] == "pending"
    # The original ask is untouched -- only the answer slot changes.
    assert reopened["question"] == "q"


def test_reopen_question_folds_prior_answer_and_followup_into_updates(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.answer_question(mission["mission_id"], entry["entry_id"], "first answer", "auto-x", db_path=path)

    db.reopen_question(mission["mission_id"], entry["entry_id"], "not quite -- what about X?", "A", db_path=path)

    updates = db.list_conversation_updates(entry["entry_id"], db_path=path)
    texts = [u["text"] for u in updates]
    assert texts == [
        "Previous answer: first answer",
        "A followed up: not quite -- what about X?",
    ]


def test_reopen_question_returns_none_when_not_yet_answered(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)

    assert db.reopen_question(mission["mission_id"], entry["entry_id"], "wait", "A", db_path=path) is None


def test_reopen_question_returns_none_for_unknown_entry(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)

    assert db.reopen_question(mission["mission_id"], "nope", "wait", "A", db_path=path) is None


def test_reopen_question_then_reanswer_leaves_no_trail_in_updates(tmp_path):
    """The whole point: after a reopen -> re-answer cycle, the ephemeral
    trail this test just populated must not linger -- see
    reopen_question's docstring and _question_payload's answered-hides-
    updates rule in the API layer."""
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.answer_question(mission["mission_id"], entry["entry_id"], "first answer", "auto-x", db_path=path)
    db.reopen_question(mission["mission_id"], entry["entry_id"], "not quite", "A", db_path=path)
    assert db.list_conversation_updates(entry["entry_id"], db_path=path)  # populated mid-discussion

    reanswered = db.answer_question(
        mission["mission_id"], entry["entry_id"], "integrated final answer", "auto-x", db_path=path,
    )

    assert reanswered["answer"] == "integrated final answer"
    # answer_question doesn't clear the updates table itself -- the API
    # layer is what hides them once answer IS NOT NULL (see test_api.py).
    # This test just pins that the DAO layer doesn't silently wipe history
    # a debugging pass might still want.
    assert db.list_conversation_updates(entry["entry_id"], db_path=path)


# ── Cross-pillar decision log ────────────────────────────────────


def test_decision_log_includes_mission_and_pillar_revisions(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>m</html>", "mission rev", db_path=path)
    db.push_pillar_site_revision(pillar["pillar_id"], "<html>p</html>", "pillar rev", db_path=path)

    log = db.list_decision_log(mission["mission_id"], db_path=path)
    texts = {entry["text"] for entry in log}
    assert texts == {"mission rev", "pillar rev"}
    pillar_entry = next(e for e in log if e["text"] == "pillar rev")
    assert pillar_entry["pillar_id"] == pillar["pillar_id"]
    mission_entry = next(e for e in log if e["text"] == "mission rev")
    assert mission_entry["pillar_id"] is None


def test_decision_log_includes_answered_questions_only(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.answer_question(mission["mission_id"], entry["entry_id"], "answered!", "auto-x", db_path=path)
    db.ask_question(mission["mission_id"], "still open", "guest:b", "B", db_path=path)

    log = db.list_decision_log(mission["mission_id"], db_path=path)
    texts = [entry["text"] for entry in log]
    assert "answered!" in texts
    assert "still open" not in texts


def test_decision_log_orders_newest_first(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v1</html>", "first", db_path=path)
    db.push_site_revision(mission["mission_id"], "<html>v2</html>", "second", db_path=path)

    log = db.list_decision_log(mission["mission_id"], db_path=path)
    assert [entry["text"] for entry in log] == ["second", "first"]


def test_decision_log_scoped_to_its_own_mission(tmp_path):
    path = _db_path(tmp_path)
    m1 = db.create_mission("A", db_path=path)
    m2 = db.create_mission("B", db_path=path)
    db.push_site_revision(m1["mission_id"], "<html>a</html>", "a-rev", db_path=path)
    db.push_site_revision(m2["mission_id"], "<html>b</html>", "b-rev", db_path=path)

    log = db.list_decision_log(m1["mission_id"], db_path=path)
    assert [entry["text"] for entry in log] == ["a-rev"]


def test_decision_log_respects_limit(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    for i in range(5):
        db.push_site_revision(mission["mission_id"], f"<html>{i}</html>", f"rev{i}", db_path=path)

    log = db.list_decision_log(mission["mission_id"], limit=2, db_path=path)
    assert len(log) == 2


def test_decision_log_empty_mission_returns_empty_list(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)
    assert db.list_decision_log(mission["mission_id"], db_path=path) == []


# ── Idle-nag support ──────────────────────────────────────────────


def test_coordinator_nag_state_round_trip(tmp_path):
    path = _db_path(tmp_path)
    assert db.get_coordinator_nag_state("auto-x", db_path=path) is None
    nagged_at = db.mark_coordinator_nagged("auto-x", db_path=path)
    assert db.get_coordinator_nag_state("auto-x", db_path=path) == nagged_at


def test_coordinator_nag_state_upserts(tmp_path):
    path = _db_path(tmp_path)
    first = db.mark_coordinator_nagged("auto-x", db_path=path)
    second = db.mark_coordinator_nagged("auto-x", db_path=path)
    assert second >= first
    assert db.get_coordinator_nag_state("auto-x", db_path=path) == second


def test_list_open_questions_for_session_spans_missions_and_pillars(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", "auto-x", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", "auto-x", db_path=path)
    db.ask_question(mission["mission_id"], "mission q", "guest:a", "A", db_path=path)
    db.ask_question(mission["mission_id"], "pillar q", "guest:b", "B", pillar_id=pillar["pillar_id"], db_path=path)

    open_qs = db.list_open_questions_for_session("auto-x", db_path=path)
    assert {q["question"] for q in open_qs} == {"mission q", "pillar q"}


def test_list_open_questions_for_session_excludes_answered(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", "auto-x", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.answer_question(mission["mission_id"], entry["entry_id"], "ans", "auto-x", db_path=path)

    assert db.list_open_questions_for_session("auto-x", db_path=path) == []


def test_list_coordinators_with_open_questions_groups_by_session(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", "auto-top", db_path=path)
    pillar = db.create_pillar(mission["mission_id"], "P", "auto-pillar", db_path=path)
    db.ask_question(mission["mission_id"], "mission q", "guest:a", "A", db_path=path)
    db.ask_question(mission["mission_id"], "pillar q", "guest:b", "B", pillar_id=pillar["pillar_id"], db_path=path)

    grouped = db.list_coordinators_with_open_questions(db_path=path)
    assert set(grouped.keys()) == {"auto-top", "auto-pillar"}
    assert grouped["auto-top"][0]["question"] == "mission q"
    assert grouped["auto-pillar"][0]["question"] == "pillar q"


def test_list_coordinators_with_open_questions_excludes_answered(tmp_path):
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", "auto-x", db_path=path)
    entry = db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    db.answer_question(mission["mission_id"], entry["entry_id"], "ans", "auto-x", db_path=path)

    assert db.list_coordinators_with_open_questions(db_path=path) == {}


def test_list_coordinators_with_open_questions_empty_when_no_coordinator_set(tmp_path):
    """A mission/pillar with no coordinator_session set can't be nagged --
    the bulk query must not crash or produce a bogus '' key."""
    path = _db_path(tmp_path)
    mission = db.create_mission("OSS Insights", db_path=path)  # no coordinator_session
    db.ask_question(mission["mission_id"], "q", "guest:a", "A", db_path=path)
    assert db.list_coordinators_with_open_questions(db_path=path) == {}
