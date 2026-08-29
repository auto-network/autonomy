"""Temporary roster-designated tunnel serving (auto-gx2mt.1)."""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.fleet_tunnel_server import (
    FLEET_TUNNEL_SERVER_REVISION,
    FLEET_TUNNEL_SERVER_SET_ID,
)
from tools.graph.schemas.machine_identity import (
    MACHINE_IDENTITY_KEY,
    MACHINE_IDENTITY_REVISION,
    MACHINE_IDENTITY_SET_ID,
)
from tools.graph.schemas.personal_identity import (
    PERSONAL_IDENTITY_REVISION,
    PERSONAL_IDENTITY_SET_ID,
)
from tools.network import fleet_roster, fleet_tunnel_server
from tools.network.idkit import KeyPair


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db(
        "machine", type_="personal", path=orgs.parent / "machine.db"
    ).close()
    yield
    GraphDB.close_all_pooled()


def _initialize_root() -> KeyPair:
    root = KeyPair.generate()
    armor = mint_password_armor(root, "test-passphrase", iterations=10_000)
    with settings_ops.identity_write_context():
        settings_ops.add_setting(
            PERSONAL_IDENTITY_SET_ID,
            PERSONAL_IDENTITY_REVISION,
            "default",
            {
                "armored_private_key": armor,
                "root_pub": root.public_hex,
                "display_name": "Fleet Test",
                "created_at": "2026-08-21T00:00:00Z",
            },
            org=None,
            state="raw",
        )
    return root


def _enroll(root: KeyPair, machine_id: str):
    entry = fleet_roster.enroll(
        root,
        machine_id=machine_id,
        machine_pub=KeyPair.generate().public_hex,
        assignment=fleet_roster.FLEET_MEMBER_ASSIGNMENT,
    )
    fleet_roster.store_entry(entry, org=None)
    return entry


def _set_local(machine_id: str) -> None:
    settings_ops.upsert_by_key(
        MACHINE_IDENTITY_SET_ID,
        MACHINE_IDENTITY_REVISION,
        MACHINE_IDENTITY_KEY,
        {"machine_id": machine_id},
        org="machine",
        state="raw",
    )


def test_pre_fleet_installation_preserves_legacy_single_node_serving(fleet):
    state = fleet_tunnel_server.state()
    assert state.allowed is True
    assert state.managed is False
    assert state.reason == "legacy-unmanaged"


def test_one_initialized_member_is_selected_implicitly(fleet):
    root = _initialize_root()
    machine_id = "11" * 32
    _enroll(root, machine_id)
    _set_local(machine_id)

    state = fleet_tunnel_server.state()
    assert state.allowed is True
    assert state.managed is True
    assert state.reason == "single-member-implicit"
    assert state.selected_machine_id == state.local_machine_id == machine_id


def test_multiple_members_fail_closed_until_one_is_selected(fleet):
    root = _initialize_root()
    first, second = "11" * 32, "22" * 32
    _enroll(root, first)
    _enroll(root, second)
    _set_local(first)

    state = fleet_tunnel_server.state()
    assert state.allowed is False
    assert state.reason == "tunnel-server-unassigned"
    assert state.active_machine_count == 2


def test_synced_selection_allows_only_the_matching_roster_machine(fleet):
    root = _initialize_root()
    selected, other = "11" * 32, "22" * 32
    _enroll(root, selected)
    _enroll(root, other)
    _set_local(selected)
    setting_id = fleet_tunnel_server.select(selected)

    chosen = fleet_tunnel_server.state()
    assert chosen.allowed is True
    assert chosen.reason == "selected"
    assert chosen.selected_machine_id == selected
    rows = settings_ops.read_owned_set(
        FLEET_TUNNEL_SERVER_SET_ID,
        org=None,
        target_revision=FLEET_TUNNEL_SERVER_REVISION,
    ).members
    assert [row.id for row in rows] == [setting_id]
    assert rows[0].state == "raw"

    _set_local(other)
    unchosen = fleet_tunnel_server.state()
    assert unchosen.allowed is False
    assert unchosen.reason == "not-designated"
    assert unchosen.selected_machine_id == selected
    assert unchosen.local_machine_id == other


def test_unknown_or_kicked_selection_is_refused(fleet):
    root = _initialize_root()
    selected, survivor = "11" * 32, "22" * 32
    entry = _enroll(root, selected)
    _enroll(root, survivor)
    _set_local(selected)

    with pytest.raises(
        fleet_tunnel_server.FleetTunnelServerError,
        match="not active",
    ):
        fleet_tunnel_server.select("99" * 32)

    fleet_tunnel_server.select(selected)
    fleet_roster.store_entry(
        fleet_roster.kick(
            root,
            machine_id=entry.machine_id,
            machine_pub=entry.machine_pub,
            assignment=entry.assignment,
            seq=entry.seq + 1,
        ),
        org=None,
    )
    state = fleet_tunnel_server.state()
    assert state.allowed is False
    assert state.reason == "assignment-inactive"


def test_partial_sync_never_falls_back_to_legacy_serving(fleet):
    """Local identity without its roster is initialized Fleet, not legacy."""
    _set_local("11" * 32)
    state = fleet_tunnel_server.state()
    assert state.allowed is False
    assert state.managed is True
    assert state.reason == "roster-empty"


def test_roster_growth_materializes_the_existing_implicit_selection(fleet):
    root = _initialize_root()
    existing, joining = "11" * 32, "22" * 32
    _enroll(root, existing)
    _set_local(existing)

    assert fleet_tunnel_server.state().reason == "single-member-implicit"
    assert fleet_tunnel_server.preserve_single_member_selection() == existing
    _enroll(root, joining)

    state = fleet_tunnel_server.state()
    assert state.allowed is True
    assert state.reason == "selected"
    assert state.selected_machine_id == existing


def test_initialized_roster_without_local_machine_identity_fails_closed(fleet):
    root = _initialize_root()
    selected = "11" * 32
    _enroll(root, selected)

    state = fleet_tunnel_server.state()
    assert state.allowed is False
    assert state.reason == "machine-identity-missing"
    assert state.selected_machine_id == selected


def test_joining_marker_fails_closed_before_roster(fleet):
    """A machine that began joining fails closed before its roster arrives.

    The durable ``fleet-joining`` marker (written at invite presentation, before
    any machine_id or roster exists) must keep a partially-enrolled or copied
    node from mistaking itself for a legacy single-node install and serving.
    """
    from tools.network import machine_boot
    from tools.network.fleet_invite import FleetInvite

    machine_boot.mark_joining(
        FleetInvite(
            personal_root_pub="ab" * 32,
            rendezvous="https://relay.example/l/" + "cd" * 16,
            invite_id="ef" * 32,
            expires_at=0,
            signature="00" * 64,
        ),
        org="machine",
    )
    state = fleet_tunnel_server.state()
    assert state.allowed is False
    assert state.managed is True
    assert state.reason == "fleet-member-provisioning"
