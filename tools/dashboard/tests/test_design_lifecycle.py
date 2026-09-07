"""Design lifecycle: automatic archiving of quiet designs and the live badge."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools.dashboard import design_lifecycle, design_shares


@pytest.fixture
def design_db(tmp_path, monkeypatch):
    from agents import design_db as db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "experiments.db")
    monkeypatch.setattr(db, "_initialized", False)
    return db


def _backdate(db, revision_id: str, when: datetime) -> None:
    conn = db._get_conn()
    try:
        conn.execute(
            "UPDATE designs SET created_at = ? WHERE id = ?",
            (when.strftime("%Y-%m-%d %H:%M:%S"), revision_id),
        )
        conn.commit()
    finally:
        conn.close()


def _status(db, revision_id: str) -> str:
    return db.get_design(revision_id)["status"]


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _make(db, title, *, age_days, session="", org="autonomy", design_id=None):
    rev = db.create_design(
        title=title, description="", fixture=None,
        variants=[{"id": "v", "html": f"<p>{title}</p>"}],
        creator_session_id=session or None, org=org, design_id=design_id,
    )
    _backdate(db, rev, NOW - timedelta(days=age_days))
    return rev


def test_parse_created_at_accepts_sqlite_and_iso_forms():
    assert design_lifecycle.parse_created_at("2026-09-01 10:00:00") == datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    assert design_lifecycle.parse_created_at("2026-09-01T10:00:00Z") == datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    assert design_lifecycle.parse_created_at("") is None
    assert design_lifecycle.parse_created_at("not a date") is None


def test_sweep_archives_quiet_designs_and_keeps_live_and_shared_ones(design_db, monkeypatch):
    quiet = _make(design_db, "Quiet", age_days=40, session="auto-dead")
    fresh = _make(design_db, "Fresh", age_days=3, session="auto-dead")
    live = _make(design_db, "Live", age_days=40, session="auto-live")
    shared = _make(design_db, "Shared", age_days=40, session="auto-dead")
    first = _make(design_db, "Revived", age_days=60, session="auto-dead")
    revived = _make(design_db, "Revived", age_days=2, session="auto-dead", design_id=first)

    monkeypatch.setattr(design_shares, "shared_design_ids", lambda org, now=None: {shared} if org == "autonomy" else set())
    result = design_lifecycle.sweep(now=NOW, live={"auto-live"})

    assert result["archived"] == [quiet]
    assert result["kept_live"] == 1 and result["kept_shared"] == 1
    assert result["checked"] == 5
    assert _status(design_db, quiet) == "dismissed"
    assert _status(design_db, fresh) == "pending"
    assert _status(design_db, live) == "pending"
    assert _status(design_db, shared) == "pending"
    # A design whose latest revision is fresh is active regardless of its first revision's age.
    assert _status(design_db, first) == "pending" and _status(design_db, revived) == "pending"

    # Idempotent: a second sweep finds nothing new.
    again = design_lifecycle.sweep(now=NOW, live={"auto-live"})
    assert again["archived"] == []


def test_sweep_archives_every_pending_revision_of_the_series(design_db, monkeypatch):
    first = _make(design_db, "Old", age_days=50)
    second = _make(design_db, "Old", age_days=45, design_id=first)
    monkeypatch.setattr(design_shares, "shared_design_ids", lambda org, now=None: set())
    design_lifecycle.sweep(now=NOW, live=set())
    assert _status(design_db, first) == "dismissed"
    assert _status(design_db, second) == "dismissed"


def test_sweep_leaves_already_archived_designs_alone(design_db, monkeypatch):
    rev = _make(design_db, "Done", age_days=90)
    design_db.dismiss_design(rev)
    monkeypatch.setattr(design_shares, "shared_design_ids", lambda org, now=None: set())
    result = design_lifecycle.sweep(now=NOW, live=set())
    assert result["archived"] == [] and result["checked"] == 0


def test_live_design_count_counts_active_designs_with_a_live_last_session(design_db):
    _make(design_db, "A", age_days=1, session="auto-live")
    _make(design_db, "B", age_days=1, session="auto-dead")
    archived = _make(design_db, "C", age_days=1, session="auto-live")
    design_db.dismiss_design(archived)
    assert design_lifecycle.live_design_count(live={"auto-live"}) == 1
    assert design_lifecycle.live_design_count(live=set()) == 0


def test_share_for_design_matches_design_or_revision_ids():
    grants = [
        {"target_uuid": "rev-2", "target_type": "design", "token": "t1", "issued_at": "2026-09-01T00:00:00Z"},
        {"target_uuid": "other", "target_type": "present", "token": "t2", "issued_at": "2026-09-02T00:00:00Z"},
    ]
    state = design_shares.share_for_design("autonomy", "design-1", ["rev-1", "rev-2"], grants=grants)
    assert state["shared"] is True
    assert [g["token"] for g in state["grants"]] == ["t1"]
    assert design_shares.share_for_design("autonomy", "design-9", [], grants=grants)["shared"] is False


def test_active_design_grants_filters_types_and_expiry(monkeypatch):
    from types import SimpleNamespace

    from tools.graph import settings_ops

    rows = [
        SimpleNamespace(payload={"target_type": "design", "target_uuid": "d1", "token": "a",
                                 "issued_at": "2026-09-01T00:00:00Z", "meta": {"ttl": 3600}}),
        SimpleNamespace(payload={"target_type": "present", "target_uuid": "d2", "token": "b",
                                 "issued_at": "2026-09-06T00:00:00Z", "meta": {"ttl": 30 * 86400, "label": "deck"}}),
        SimpleNamespace(payload={"target_type": "note", "target_uuid": "n1", "token": "c",
                                 "issued_at": "2026-09-06T00:00:00Z", "meta": {}}),
        SimpleNamespace(payload="garbage"),
    ]
    monkeypatch.setattr(settings_ops, "read_owned_set", lambda *a, **k: SimpleNamespace(members=rows))
    grants = design_shares.active_design_grants("autonomy", now=NOW)
    assert [g["token"] for g in grants] == ["b"]        # 'a' expired, 'c' wrong type
    assert grants[0]["label"] == "deck"
    assert design_shares.shared_design_ids("autonomy", now=NOW) == {"d2"}
    assert design_shares.active_design_grants(None) == []


def test_share_for_target_treats_design_and_present_grants_alike_and_others_strictly(monkeypatch):
    grants = [
        {"target_uuid": "deck-1", "target_type": "present", "token": "p", "issued_at": "2026-09-01T00:00:00Z"},
        {"target_uuid": "note-1", "target_type": "note", "token": "n", "issued_at": "2026-09-02T00:00:00Z"},
    ]
    monkeypatch.setattr(design_shares, "active_grants",
                        lambda org, types=design_shares.SHARE_TARGET_TYPES, now=None: [g for g in grants if g["target_type"] in set(types)])
    assert design_shares.share_for_target("autonomy", "design", "deck-1")["shared"] is True    # present grant reaches the design
    assert design_shares.share_for_target("autonomy", "note", "note-1")["grants"][0]["token"] == "n"
    assert design_shares.share_for_target("autonomy", "note", "deck-1")["shared"] is False    # a deck grant is not a note grant
    assert design_shares.share_for_target("autonomy", "present", "other", ["deck-1"])["shared"] is True
