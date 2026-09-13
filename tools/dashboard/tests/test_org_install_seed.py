"""The install seed never replicates (finding 2026-09-13).

A joiner installs the sponsor's directory and reachability rows into the
MACHINE store, not into its copy of the organization-homed sets: written
there they would be this member's authored writes and sync back to every
member under its identity. The two readers overlay the seed only where no
replicated row exists yet; the replicated row shadows it once it arrives.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tools.dashboard import member_directory, org_install_seed, org_membership_routes
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.org_fleet_reachability import ORG_FLEET_REACHABILITY_SET_ID
from tools.graph.schemas.org_member_profile import MEMBER_PROFILE_SET_ID
from tools.network import fleet_org_reachability as reach
from tools.network.fleet_sync.tests.test_org_channel_routing import ORG, Member
from tools.network.idkit import KeyPair

SLUG = "acme"
ICON = "data:image/png;base64,AAAA"


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db(SLUG).close()
    yield tmp_path / "orgs"
    GraphDB.close_all_pooled()


def _org_rows(set_id: str) -> dict:
    return settings_ops.read_owned_set(set_id, org=SLUG).to_dict()


def test_seed_lands_in_the_machine_store_and_never_in_the_org_sets(stores, tmp_path):
    pa, pb = KeyPair.generate(), KeyPair.generate()
    alice = Member(tmp_path, "alice", pa, [pa.public_hex, pb.public_hex])
    now = int(time.time())
    row = reach.build_row(alice.machine, alice.persona_cert, ["ws://10.0.0.1:9"], now=now)
    tampered = {**row, "addresses": ["ws://6.6.6.6:1"]}
    counts = org_install_seed.install(
        SLUG, ORG,
        member_profiles=[
            {"persona_pub": pa.public_hex, "display_name": "Alice", "byline": "Founder", "avatar": ICON},
            {"persona_pub": pb.public_hex, "display_name": "Bob"},  # the joiner: never seeded
            {"persona_pub": "c" * 64, "display_name": ""},        # nameless
            "junk",
        ],
        reachability_rows=[
            {"key": alice.machine.public_hex, **row},
            {"key": alice.machine.public_hex, **tampered},          # signature broken
            {"key": "short", **row},
        ],
        skip_persona=pb.public_hex, now=now,
    )
    assert counts == {"member_profile": 1, "reachability": 1}
    # Nothing reached the organization-homed sets: they still hold no rows.
    assert _org_rows(MEMBER_PROFILE_SET_ID) == {}
    assert _org_rows(ORG_FLEET_REACHABILITY_SET_ID) == {}
    # The seed is what the readers overlay.
    assert org_install_seed.seed_profiles(SLUG) == {pa.public_hex: {
        "display_name": "Alice", "byline": "Founder", "avatar": ICON, "color": None,
    }}
    assert org_install_seed.seed_reachability(
        SLUG, org=ORG, own_machine_pub=pb.public_hex, now=now,
    ) == {alice.machine.public_hex: ["ws://10.0.0.1:9"]}
    # Another org's seed and this machine's own row are not co-members.
    assert org_install_seed.seed_reachability("other", org=ORG, now=now) == {}
    assert org_install_seed.seed_reachability(
        SLUG, org=ORG, own_machine_pub=alice.machine.public_hex, now=now,
    ) == {}
    # The seed does not verify against another organization's genesis.
    assert org_install_seed.seed_reachability(SLUG, org="f" * 64, now=now) == {}


def test_replicated_directory_row_shadows_the_seed(stores, monkeypatch):
    alice, bob = "a" * 64, "b" * 64
    org_install_seed.install(
        SLUG, ORG,
        member_profiles=[{"persona_pub": alice, "display_name": "Alice (seed)"}],
        reachability_rows=[], skip_persona=bob,
    )
    monkeypatch.setattr(
        member_directory, "presentation_from_personal_profile",
        lambda: {"display_name": "Bob", "byline": "", "avatar": ICON, "color": ""},
    )
    assert member_directory.write_self(SLUG, bob) is True
    profiles = org_membership_routes._member_profiles(SLUG)
    assert profiles[alice]["display_name"] == "Alice (seed)"
    assert profiles[bob] == {"display_name": "Bob", "avatar": ICON, "color": None, "byline": None}
    # Alice's own row arrives by replication (photo included): it wins.
    member_directory.write_row(SLUG, alice, {"display_name": "Alice", "avatar": ICON})
    profiles = org_membership_routes._member_profiles(SLUG)
    assert profiles[alice] == {"display_name": "Alice", "avatar": ICON, "color": None, "byline": None}
    # Bob's own row is the only one his machine authored into the org set.
    assert set(_org_rows(MEMBER_PROFILE_SET_ID)) == {alice, bob}
