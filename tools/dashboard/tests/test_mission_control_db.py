from __future__ import annotations

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
