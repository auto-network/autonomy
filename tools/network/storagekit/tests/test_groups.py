"""The group derived fold: replay(group events) ∩ parent roster.

Pins the operator-ratified subgroup properties (design
``graph://fe4499fa-0e9``): temporal independence, cascade on parent
removal, the member-outside-the-org unrepresentability, the admin-set
admission predicate (a non-admin's record is not an event), safety-biased
remove-wins ties, tamper and cross-charter rejection, and the
recipient-resolution seam that mirrors ``_current_member_credentials``.
"""

from __future__ import annotations

import dataclasses
import os

from tools.network.idkit import KeyPair

from tools.network.storagekit.errors import RecordSignatureError
from tools.network.storagekit.groups import (
    accept_charter,
    create_charter,
    group_member_credentials,
    group_member_keys,
    make_group_event,
    replay_group,
)
from tools.network.storagekit.tests.conftest import World

HLC0 = (1_800_000_000_000, 0)
HLC1 = (1_800_000_000_001, 0)
HLC2 = (1_800_000_000_002, 0)


def _gid() -> str:
    return os.urandom(32).hex()


def _pub(world: World, index: int) -> str:
    return world.member(index).public_hex


class _Credentials:
    """The credentials_for_persona seam, backed by the World's principals."""

    def __init__(self, world: World):
        self._world = world

    def credentials_for_persona(self, persona: str) -> list:
        entry = self._world.principals.get(persona)
        return [entry["credential"]] if entry else []


def test_initial_members_intersect_the_parent_roster():
    world = World(member_count=3)
    alice, bob = _pub(world, 0), _pub(world, 1)
    charter = create_charter(
        world.member(0), _gid(), "infra", [alice], [alice, bob], HLC0
    )
    accept_charter(world.fold(), charter)
    assert group_member_keys(world.fold(), charter, []) == {alice, bob}


def test_temporal_independence_admits_a_later_org_joiner():
    world = World(member_count=2)
    alice = _pub(world, 0)
    charter = create_charter(world.member(0), _gid(), "infra", [alice], [alice], HLC0)
    # Carol joins the ORGANIZATION only after the charter exists.
    carol = world.admit(seed_index=70)
    add = make_group_event(world.member(0), charter, "add", carol.public_hex, HLC1)
    members = group_member_keys(world.fold(), charter, [add])
    assert carol.public_hex in members, (
        "the genesis never needed to know a later joiner — the fold joins the "
        "two logs by reference at fold time"
    )


def test_org_removal_cascades_with_no_group_event():
    world = World(member_count=3)
    alice, bob = _pub(world, 0), _pub(world, 1)
    charter = create_charter(
        world.member(0), _gid(), "infra", [alice], [alice, bob], HLC0
    )
    assert bob in group_member_keys(world.fold(), charter, [])
    world.remove(world.member(1))
    assert bob not in group_member_keys(world.fold(), charter, []), (
        "membership is an intersection with the live parent, not a copy"
    )
    # The group's own log still references him; only the intersection dropped.
    assert bob in replay_group(charter, [])


def test_a_non_org_member_is_unrepresentable():
    world = World(member_count=2)
    alice = _pub(world, 0)
    outsider = KeyPair.generate().public_hex
    charter = create_charter(world.member(0), _gid(), "infra", [alice], [alice], HLC0)
    add = make_group_event(world.member(0), charter, "add", outsider, HLC1)
    assert outsider in replay_group(charter, [add])
    assert outsider not in group_member_keys(world.fold(), charter, [add])


def test_a_non_admins_record_is_not_an_event():
    world = World(member_count=3)
    alice, carol = _pub(world, 0), _pub(world, 2)
    charter = create_charter(world.member(0), _gid(), "infra", [alice], [alice], HLC0)
    # Bob is a full organization member but not a charter admin.
    forged = make_group_event(world.member(1), charter, "add", carol, HLC1)
    assert carol not in group_member_keys(world.fold(), charter, [forged])


def test_remove_outranks_add_at_an_identical_hlc():
    world = World(member_count=3)
    alice, carol = _pub(world, 0), _pub(world, 2)
    charter = create_charter(world.member(0), _gid(), "infra", [alice], [alice], HLC0)
    add = make_group_event(world.member(0), charter, "add", carol, HLC1)
    remove = make_group_event(world.member(0), charter, "remove", carol, HLC1)
    for order in ([add, remove], [remove, add]):
        assert carol not in group_member_keys(world.fold(), charter, order)


def test_last_operation_per_subject_wins_across_hlcs():
    world = World(member_count=3)
    alice, carol = _pub(world, 0), _pub(world, 2)
    charter = create_charter(world.member(0), _gid(), "infra", [alice], [alice], HLC0)
    add = make_group_event(world.member(0), charter, "add", carol, HLC1)
    remove = make_group_event(world.member(0), charter, "remove", carol, HLC0)
    # The remove predates the add; arrival order must not matter.
    for order in ([add, remove], [remove, add]):
        assert carol in group_member_keys(world.fold(), charter, order)


def test_a_tampered_event_is_not_an_event():
    world = World(member_count=3)
    alice, bob, carol = _pub(world, 0), _pub(world, 1), _pub(world, 2)
    charter = create_charter(world.member(0), _gid(), "infra", [alice], [alice], HLC0)
    add = make_group_event(world.member(0), charter, "add", carol, HLC1)
    swapped = dataclasses.replace(add, subject=bob)
    members = group_member_keys(world.fold(), charter, [swapped])
    assert bob not in members and carol not in members


def test_an_event_binds_to_its_exact_charter():
    world = World(member_count=3)
    alice, carol = _pub(world, 0), _pub(world, 2)
    charter_a = create_charter(world.member(0), _gid(), "a", [alice], [alice], HLC0)
    charter_b = create_charter(world.member(0), _gid(), "b", [alice], [alice], HLC0)
    add = make_group_event(world.member(0), charter_a, "add", carol, HLC1)
    assert carol not in group_member_keys(world.fold(), charter_b, [add])


def test_charter_acceptance_requires_a_current_member_author():
    world = World(member_count=2)
    alice = _pub(world, 0)
    outsider = KeyPair.generate()
    good = create_charter(world.member(0), _gid(), "infra", [alice], [alice], HLC0)
    accept_charter(world.fold(), good)
    bad = create_charter(outsider, _gid(), "rogue", [outsider.public_hex], [], HLC0)
    try:
        accept_charter(world.fold(), bad)
    except RecordSignatureError:
        pass
    else:
        raise AssertionError("an outsider's charter must be refused")


def test_recipients_mirror_the_member_credential_seam():
    world = World(member_count=3)
    alice, bob = _pub(world, 0), _pub(world, 1)
    charter = create_charter(
        world.member(0), _gid(), "infra", [alice], [alice, bob], HLC0
    )
    creds = group_member_credentials(
        world.fold(), charter, [], _Credentials(world), world.ancestry
    )
    assert sorted(c.persona for c in creds) == sorted([alice, bob])
    # Cascade reaches the recipient set with no group event.
    world.remove(world.member(1))
    creds = group_member_credentials(
        world.fold(), charter, [], _Credentials(world), world.ancestry
    )
    assert [c.persona for c in creds] == [alice]
