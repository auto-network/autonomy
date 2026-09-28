"""Session presence (graph://7eb29bc8-31a §9.5, bead auto-jh50w): the writer
writes only differences, deprecates ended sessions, and the reader marks
another machine's sessions unreachable when that machine has not been pulled
recently."""

from __future__ import annotations

import pytest

from tools.dashboard import session_presence as sp
from tools.graph.schemas.personal_session_presence import (
    PersonalSessionPresenceV1,
    SchemaValidationError,
    presence_key,
    split_key,
)

HOME = sp.LocalMachine(machine_pub="a1" * 32, machine_id="a2" * 32)
SJC = sp.LocalMachine(machine_pub="b1" * 32, machine_id="b2" * 32)
NOW = 1_800_000_000


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.setenv("GRAPH_ORG", "personal")
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    try:
        yield tmp_path
    finally:
        GraphDB.close_all_pooled()


def _session(name, **extra):
    return {"tmux_name": name, "state": "ACTIVE", "created_at": NOW - 100,
            "label": f"label {name}", "project": "autonomy-developer-opus",
            "type": "container", "harness": "claude", **extra}


ROSTER = {HOME.machine_pub: HOME.machine_id, SJC.machine_pub: SJC.machine_id}


def _rows(**kwargs):
    kwargs.setdefault("names", {HOME.machine_id: "home", SJC.machine_id: "sjc-2"})
    kwargs.setdefault("roster", ROSTER)
    kwargs.setdefault("now", NOW)
    return {r["tmux_name"]: r for r in sp.read_presence(**kwargs)}


def test_key_round_trips_and_refuses_malformed_keys():
    key = presence_key(HOME.machine_pub, "auto-0928-114556")
    assert split_key(key) == (HOME.machine_pub, "auto-0928-114556")
    assert split_key("nothex:auto-1") is None
    assert split_key(f"{HOME.machine_pub}:bad name") is None


def test_schema_refuses_a_terminal_state():
    with pytest.raises(SchemaValidationError):
        PersonalSessionPresenceV1.validate(
            {"machine_id": HOME.machine_id, "state": "ENDED", "since": 1})


def test_a_new_session_is_written_and_an_unchanged_roster_writes_nothing(store):
    live = [_session("auto-1"), _session("auto-2")]
    assert sp.reconcile(HOME, live) == {"upserted": 2, "deprecated": 0}
    assert sp.reconcile(HOME, live) == {"upserted": 0, "deprecated": 0}


def test_a_changed_session_is_rewritten(store):
    sp.reconcile(HOME, [_session("auto-1")])
    changed = [_session("auto-1", label="renamed")]
    assert sp.reconcile(HOME, changed) == {"upserted": 1, "deprecated": 0}
    assert _rows(local_pub=HOME.machine_pub)["auto-1"]["label"] == "renamed"


def test_an_ended_session_is_deprecated_and_disappears(store):
    sp.reconcile(HOME, [_session("auto-1"), _session("auto-2")])
    assert sp.reconcile(HOME, [_session("auto-2")]) == {"upserted": 0, "deprecated": 1}
    assert set(_rows(local_pub=HOME.machine_pub)) == {"auto-2"}


def test_a_machine_only_reconciles_its_own_rows(store):
    sp.reconcile(SJC, [_session("auto-9")])
    assert sp.reconcile(HOME, []) == {"upserted": 0, "deprecated": 0}
    assert "auto-9" in _rows(local_pub=HOME.machine_pub)


def test_an_invalid_tmux_name_is_skipped(store):
    assert sp.reconcile(HOME, [_session("bad name")]) == {"upserted": 0, "deprecated": 0}


def test_a_recently_pulled_peer_is_reachable(store):
    sp.reconcile(SJC, [_session("auto-9")])
    row = _rows(local_pub=HOME.machine_pub,
                peer_last_success={SJC.machine_pub: NOW - 20})["auto-9"]
    assert row["reachable"] is True
    assert row["machine"] == "sjc-2"
    assert row["local"] is False


def test_a_peer_not_pulled_recently_is_unreachable_since_its_last_pull(store):
    sp.reconcile(SJC, [_session("auto-9")])
    last = NOW - sp.REACHABLE_WINDOW_S - 1
    row = _rows(local_pub=HOME.machine_pub,
                peer_last_success={SJC.machine_pub: last})["auto-9"]
    assert row["reachable"] is False
    assert row["unreachable_since"] == last


def test_a_never_pulled_peer_is_unreachable_and_own_rows_are_live(store):
    sp.reconcile(SJC, [_session("auto-9")])
    sp.reconcile(HOME, [_session("auto-1")])
    rows = _rows(local_pub=HOME.machine_pub, peer_last_success={})
    assert rows["auto-9"]["reachable"] is False
    assert rows["auto-9"]["unreachable_since"] is None
    assert rows["auto-1"]["reachable"] is True


def test_remote_status_rows_use_the_name_at_machine_address(store):
    sp.reconcile(SJC, [_session("auto-9")])
    sp.reconcile(HOME, [_session("auto-1")])
    rows = sp.remote_status_rows(
        local_pub=HOME.machine_pub,
        peer_last_success={SJC.machine_pub: NOW - 5},
        names={SJC.machine_id: "sjc-2"}, roster=ROSTER,
        now=NOW,
    )
    assert [r["tmux_name"] for r in rows] == ["auto-9@sjc-2"]
    assert rows[0]["attention"] == "remote"
    unreachable = sp.remote_status_rows(
        local_pub=HOME.machine_pub, peer_last_success={},
        names={SJC.machine_id: "sjc-2"}, roster=ROSTER, now=NOW,
    )
    assert unreachable[0]["attention"] == "unreach"


def test_a_row_under_one_pub_carrying_another_machine_id_is_dropped(store):
    forged = sp.LocalMachine(machine_pub=SJC.machine_pub, machine_id=HOME.machine_id)
    sp.reconcile(forged, [_session("auto-9")])
    assert "auto-9" not in _rows(local_pub=HOME.machine_pub)


def test_a_row_for_a_machine_not_in_the_roster_is_dropped(store):
    stranger = sp.LocalMachine(machine_pub="c1" * 32, machine_id="c2" * 32)
    sp.reconcile(stranger, [_session("auto-7")])
    assert "auto-7" not in _rows(local_pub=HOME.machine_pub)


def test_no_roster_identity_lists_nothing(store):
    sp.reconcile(SJC, [_session("auto-9")])
    assert sp.read_presence(local_pub=HOME.machine_pub, roster=None,
                            peer_last_success={}, names={}, now=NOW) == []
