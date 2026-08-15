"""The boundary that matters: another principal reads what I wrote.

A local round trip proves the encryption composes. It does not prove the
thing a vault is for, which is that a secret written on one node, in one
store, becomes readable by a DIFFERENT member on a DIFFERENT store -- and by
nobody else. So this writes on one store, replicates the object bytes to a
second, delivers the key by capability grant, and reads it back as someone
else.

Replication here is deliberately dumb: the object's header and body are
copied verbatim, the way any replicator would move them. Nothing about the
transport is trusted -- if the bytes were enough to read the secret, the
encryption would be pointless.
"""

from __future__ import annotations

import pytest

from tools.graph.vault import open_setting, parse_locator, seal_setting
from tools.network.idkit import KeyPair
from tools.network.storagekit import capability
from tools.network.storagekit.credentials import build as build_credential
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.tests.test_objects import World

SET_ID = "dashboard.claude.credentials"
KEY = "default"
SECRET = {"access_token": "sk-ant-EXAMPLE-never-real", "refresh": "rt-EXAMPLE"}


@pytest.fixture
def world():
    return World()


@pytest.fixture
def stores(tmp_path):
    """Two SEPARATE stores -- one per node, as replication actually works."""
    a = ContentStore(tmp_path / "node-a")
    b = ContentStore(tmp_path / "node-b")
    yield a, b
    a.close()
    b.close()


def replicate(src, dst, object_id, revision_id):
    """Move one object between stores, byte for byte, trusting nothing."""
    header, body = src.get_object(object_id, revision_id)
    dst.put_object(header, body)
    return header, body


def test_another_principal_on_another_store_reads_what_was_written(world, stores):
    node_a, node_b = stores

    # ── Alice writes the secret on node A ─────────────────────────────────
    reference = seal_setting(
        author=world.member,
        frontier=world.f1,
        setting_id="row-1",
        set_id=SET_ID,
        key=KEY,
        payload=SECRET,
        held_secrets={world.s1.state_id: world.sec1},
        available_states={world.s1.state_id: world.s1},
        ancestry=world.ancestry,
        store=node_a,
        bridges=world.bridges,
        descriptors=world.descriptors,
    )

    # ── The object replicates to node B ───────────────────────────────────
    located = parse_locator(reference)
    replicate(node_a, node_b, located["object_id"], located["revision_id"])

    # ── Bob has a credential of his own; Alice grants him the state key ───
    bob = KeyPair.generate()
    bob_credential, bob_kem_private = build_credential(
        bob, world.gen, b"bob-kem-seed".ljust(32, b"\0"),
        world.f1.heads, (1, 0),
    )
    grant = capability.issue(
        world.member,
        genesis_id=world.gen,
        domain_id=world.dom,
        storage_state_id=world.s1.state_id,
        recipient_credential=bob_credential,
        state_secret=world.sec1,
        state_secret_commitment=world.s1.secret_commitment,
        authority_heads=world.s1.authority_heads,
    )

    # ── Bob recovers the key from the grant alone, then reads on node B ───
    bob_secret = capability.accept(grant, bob_kem_private, world.s1)
    assert bob_secret == world.sec1, "the grant must deliver the real state key"

    got = open_setting(
        reference,
        held_secrets={world.s1.state_id: bob_secret},
        bridges=world.bridges,
        descriptors=world.descriptors,
        store=node_b,
    )
    assert got == SECRET, "another principal, another store -- and it opened"


def test_the_replicated_bytes_alone_do_not_reveal_the_secret(world, stores):
    """A replicator moves the object; it must not be able to read it."""
    node_a, node_b = stores
    reference = seal_setting(
        author=world.member, frontier=world.f1, setting_id="row-1",
        set_id=SET_ID, key=KEY, payload=SECRET,
        held_secrets={world.s1.state_id: world.sec1},
        available_states={world.s1.state_id: world.s1},
        ancestry=world.ancestry, store=node_a,
        bridges=world.bridges, descriptors=world.descriptors,
    )
    located = parse_locator(reference)
    _header, body = replicate(
        node_a, node_b, located["object_id"], located["revision_id"]
    )
    token = SECRET["access_token"].encode()
    assert token not in body

    # Holding the whole store and no grant reads nothing.
    with pytest.raises(Exception):
        open_setting(
            reference, held_secrets={}, bridges=world.bridges,
            descriptors=world.descriptors, store=node_b,
        )


def test_a_grant_for_someone_else_does_not_open_it(world, stores):
    """The key is delivered to ONE credential, not to whoever presents it."""
    node_a, node_b = stores
    reference = seal_setting(
        author=world.member, frontier=world.f1, setting_id="row-1",
        set_id=SET_ID, key=KEY, payload=SECRET,
        held_secrets={world.s1.state_id: world.sec1},
        available_states={world.s1.state_id: world.s1},
        ancestry=world.ancestry, store=node_a,
        bridges=world.bridges, descriptors=world.descriptors,
    )
    located = parse_locator(reference)
    replicate(node_a, node_b, located["object_id"], located["revision_id"])

    bob = KeyPair.generate()
    bob_credential, _bob_private = build_credential(
        bob, world.gen, b"bob-kem-seed".ljust(32, b"\0"), world.f1.heads, (1, 0),
    )
    grant_for_bob = capability.issue(
        world.member,
        genesis_id=world.gen, domain_id=world.dom,
        storage_state_id=world.s1.state_id,
        recipient_credential=bob_credential,
        state_secret=world.sec1,
        state_secret_commitment=world.s1.secret_commitment,
        authority_heads=world.s1.authority_heads,
    )

    # Mallory intercepts the grant and tries it with her own key.
    mallory = KeyPair.generate()
    _mallory_credential, mallory_private = build_credential(
        mallory, world.gen, b"mallory-seed".ljust(32, b"\0"), world.f1.heads, (1, 0),
    )
    with pytest.raises(Exception):
        capability.accept(grant_for_bob, mallory_private, world.s1)
