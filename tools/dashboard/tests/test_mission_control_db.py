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

    fetched = db.get_mission(mission["mission_id"], db_path=path)
    assert fetched["mission_id"] == mission["mission_id"]
    assert fetched["name"] == "OSS Insights"


def test_get_mission_missing_returns_none(tmp_path):
    path = _db_path(tmp_path)
    db.init_db(path)
    assert db.get_mission("does-not-exist", db_path=path) is None


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
