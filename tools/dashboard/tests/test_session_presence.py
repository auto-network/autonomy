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


# ── organization rosters (auto-qrmlg.8) ─────────────────────────────────────

ACME = "acme"
ACME_ID = "77777777-7777-4777-8777-777777777777"
PERSONA = "ee" * 32
ORG_MACHINE = sp.OrgSink(org=ACME, machine_pub="c1" * 32, persona_pub=PERSONA)
PEER_ORG_MACHINE = sp.OrgSink(org=ACME, machine_pub="d1" * 32, persona_pub="ff" * 32)


@pytest.fixture
def org_store(store, monkeypatch):
    from tools.graph.db import GraphDB
    GraphDB.create_org_db("personal", type_="personal", path=store / "orgs" / "personal.db").close()
    GraphDB.create_org_db(ACME, root=store / "orgs", org_id=ACME_ID).close()
    from tools.graph import settings_ops
    monkeypatch.setattr(settings_ops, "_SIGNER_PROVIDER", None)   # unsigned locally: the boundary is at sync
    # Workspace ids resolve to their organization: 'acme-dev' is acme's, the rest is personal.
    from tools.dashboard import org_identity
    monkeypatch.setattr(org_identity, "session_org_slug",
                        lambda session: ACME if session.get("project") == "acme-dev" else "personal")
    return store


BINDINGS = {ORG_MACHINE.machine_pub: ORG_MACHINE.persona_pub,
            PEER_ORG_MACHINE.machine_pub: PEER_ORG_MACHINE.persona_pub}


def _sign_as(persona: str, *, machine_pub: str | None = None) -> None:
    """Stamp the envelope the production write path puts on every organization
    row (the tests hold no delegate): signed for *persona*; optionally only
    the rows under *machine_pub*."""
    from tools.graph import settings_ops
    db = settings_ops._open(ACME)
    try:
        where, params = "set_id=?", [sp.ORG_SESSION_PRESENCE_SET_ID]
        if machine_pub is not None:
            where += " AND key LIKE ?"
            params.append(machine_pub + ":%")
        db.conn.execute(
            f"UPDATE settings SET signed_at=1, signing_key=?, signature=?, witness=NULL, "
            f"terminal_persona=? WHERE {where} AND signature IS NULL",
            ("aa" * 32, "bb" * 64, persona, *params),
        )
        db.conn.commit()
    finally:
        db.close()


def _reconcile_org(machine, live, sink):
    result = sp.reconcile(machine, live, sink=sink)
    _sign_as(sink.persona_pub, machine_pub=sink.machine_pub)
    return result


def _org_rows(**kwargs):
    kwargs.setdefault("local_pubs", {ORG_MACHINE.machine_pub})
    kwargs.setdefault("relay_slots", set())
    kwargs.setdefault("peer_last_success", {})
    kwargs.setdefault("names", {})
    kwargs.setdefault("machine_personas", BINDINGS)
    kwargs.setdefault("now", NOW)
    return {r["tmux_name"]: r for r in sp.read_org_presence(ACME, **kwargs)}


def test_an_org_roster_holds_only_that_organizations_sessions_and_names_the_member(org_store):
    live = [_session("auto-1", project="acme-dev"), _session("auto-2")]   # auto-2 is personal
    assert _reconcile_org(HOME, live, ORG_MACHINE) == {"upserted": 1, "deprecated": 0}
    rows = _org_rows()
    assert set(rows) == {"auto-1"}
    assert rows["auto-1"]["persona_pub"] == PERSONA and rows["auto-1"]["org"] == ACME
    assert rows["auto-1"]["machine_pub"] == ORG_MACHINE.machine_pub and rows["auto-1"]["local"] is True
    # The personal sink is untouched by the org reconcile, and vice versa.
    assert _rows(local_pub=HOME.machine_pub) == {}
    assert _reconcile_org(HOME, live, ORG_MACHINE) == {"upserted": 0, "deprecated": 0}
    # The session ends: deprecated, gone from the roster.
    assert _reconcile_org(HOME, [_session("auto-2")], ORG_MACHINE) == {"upserted": 0, "deprecated": 1}
    assert _org_rows() == {}


def test_a_co_members_row_is_live_only_while_its_machine_holds_a_relay_slot_or_was_pulled(org_store):
    _reconcile_org(SJC, [_session("auto-9", project="acme-dev")], PEER_ORG_MACHINE)
    # No slot, never pulled: unreachable, never live.
    row = _org_rows()["auto-9"]
    assert row["reachable"] is False and row["unreachable_since"] is None and row["local"] is False
    # A live serving slot at the org's relay makes it live.
    assert _org_rows(relay_slots={PEER_ORG_MACHINE.machine_pub})["auto-9"]["reachable"] is True
    # A recent pull of the org scope from that machine also does.
    assert _org_rows(peer_last_success={PEER_ORG_MACHINE.machine_pub: NOW - 30})["auto-9"]["reachable"] is True
    stale = _org_rows(peer_last_success={PEER_ORG_MACHINE.machine_pub: NOW - 600})["auto-9"]
    assert stale["reachable"] is False and stale["unreachable_since"] == NOW - 600


def test_a_row_naming_another_persona_than_its_signer_is_dropped(org_store):
    sp.reconcile(SJC, [_session("auto-9", project="acme-dev")], sink=PEER_ORG_MACHINE)
    # Signed for someone else than the payload names: not that member's statement.
    _sign_as("ab" * 32, machine_pub=PEER_ORG_MACHINE.machine_pub)
    assert _org_rows(relay_slots={PEER_ORG_MACHINE.machine_pub}) == {}


def test_an_unsigned_row_does_not_count_whoever_it_names(org_store):
    sp.reconcile(SJC, [_session("auto-9", project="acme-dev")], sink=PEER_ORG_MACHINE)
    # Written but never signed (a store with the require-signed flag off, or
    # a row from before it): no member stands behind it.
    assert _org_rows(relay_slots={PEER_ORG_MACHINE.machine_pub}) == {}


def test_a_row_under_another_members_machine_is_dropped(org_store):
    """Member X signs a row keyed under member Y's serving machine: with Y's
    machine live at the relay, co-members would otherwise see X's session
    live on a machine that does not run it, and route to it."""
    sp.reconcile(SJC, [_session("auto-9", project="acme-dev")], sink=PEER_ORG_MACHINE)
    _sign_as(ORG_MACHINE.persona_pub, machine_pub=PEER_ORG_MACHINE.machine_pub)   # X's signature, Y's machine
    rows = _org_rows(relay_slots={PEER_ORG_MACHINE.machine_pub})
    assert rows == {}
    # ... and a machine no binding names does not count either.
    _sign_as(PEER_ORG_MACHINE.persona_pub, machine_pub=PEER_ORG_MACHINE.machine_pub)
    assert _org_rows(machine_personas={ORG_MACHINE.machine_pub: ORG_MACHINE.persona_pub}) == {}


def test_org_status_rows_use_the_name_at_machine_address_and_carry_the_org(org_store, monkeypatch):
    _reconcile_org(SJC, [_session("auto-9", project="acme-dev")], PEER_ORG_MACHINE)
    _reconcile_org(HOME, [_session("auto-1", project="acme-dev")], ORG_MACHINE)
    monkeypatch.setattr(sp, "org_sinks", lambda: [ORG_MACHINE])
    monkeypatch.setattr(sp, "_org_relay_slots", lambda: {ACME: {PEER_ORG_MACHINE.machine_pub}})
    monkeypatch.setattr(sp, "_org_peer_last_success_s", lambda org: {})
    monkeypatch.setattr(sp, "_machine_names", lambda: {})
    monkeypatch.setattr(sp, "_machine_personas", lambda org: BINDINGS)
    rows = {r["tmux_name"]: r for r in sp.org_status_rows(now=NOW)}
    assert set(rows) == {f"auto-9@{PEER_ORG_MACHINE.machine_pub[:12]}"}     # own rows are not listed as remote
    row = rows[f"auto-9@{PEER_ORG_MACHINE.machine_pub[:12]}"]
    assert row["org"] == ACME and row["attention"] == "remote" and row["persona_pub"] == "ff" * 32


def test_reconcile_local_writes_every_sink_and_one_failing_org_does_not_stop_the_rest(org_store, monkeypatch):
    from tools.dashboard.dao import dashboard_db
    monkeypatch.setattr(sp, "local_machine", lambda: HOME)
    monkeypatch.setattr(dashboard_db, "get_live_sessions",
                        lambda **kw: [_session("auto-1", project="acme-dev"), _session("auto-2")])
    broken = sp.OrgSink(org="nowhere", machine_pub="e1" * 32, persona_pub=PERSONA)   # no store for it
    monkeypatch.setattr(sp, "org_sinks", lambda: [broken, ORG_MACHINE])
    assert sp.reconcile_local() == {"upserted": 3, "deprecated": 0}      # 2 personal + 1 org
    assert set(_rows(local_pub=HOME.machine_pub)) == {"auto-1", "auto-2"}
    _sign_as(ORG_MACHINE.persona_pub)
    assert set(_org_rows()) == {"auto-1"}
