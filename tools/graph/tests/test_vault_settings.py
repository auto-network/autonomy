"""A vault setting's payload is encrypted, and the row never holds it.

The acceptance the bead names: writing a vault secret produces a storage
object whose ciphertext appears nowhere in the settings row; the same setting
written twice produces two revisions of ONE object; and a reader without the
keys gets a refusal rather than plaintext.
"""

from __future__ import annotations

import json
import os

import pytest

from tools.graph.vault import (
    VaultError,
    build_reference,
    is_vault_reference,
    object_id_for,
    open_setting,
    parse_reference,
    revision_id_for,
    seal_setting,
)
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.tests.test_objects import World

SET_ID = "dashboard.claude.credentials"
KEY = "default"
SECRET = {"access_token": "sk-ant-oat01-EXAMPLE-not-a-real-token", "refresh": "rt-EXAMPLE"}


@pytest.fixture
def world():
    return World()


@pytest.fixture
def store(tmp_path):
    s = ContentStore(tmp_path / "vault-store")
    yield s
    s.close()


def seal(world, store, setting_id="row-1", payload=None):
    return seal_setting(
        author=world.member,
        frontier=world.f1,
        setting_id=setting_id,
        set_id=SET_ID,
        key=KEY,
        payload=SECRET if payload is None else payload,
        held_secrets={world.s1.state_id: world.sec1},
        available_states={world.s1.state_id: world.s1},
        ancestry=world.ancestry,
        store=store,
        bridges=world.bridges,
        descriptors=world.descriptors,
    )


# ── The row holds a locator, never the secret ─────────────────────────────


def test_the_settings_row_holds_no_ciphertext_and_no_secret(world, store):
    ref = seal(world, store)
    row = json.dumps(ref)
    assert SECRET["access_token"] not in row
    assert SECRET["refresh"] not in row
    # And nothing key-shaped or ciphertext-shaped rode along.
    assert set(ref) == {
        "vault", "genesis_id", "domain_id", "object_id", "revision_id",
        "storage_state_id", "policy_class",
    }
    assert is_vault_reference(ref)


def test_the_secret_is_recoverable_through_the_reference(world, store):
    ref = seal(world, store)
    got = open_setting(
        ref,
        held_secrets={world.s1.state_id: world.sec1},
        bridges=world.bridges,
        descriptors=world.descriptors,
        store=store,
    )
    assert got == SECRET


def test_the_stored_body_is_ciphertext(world, store, tmp_path):
    """What lands on disk must not contain the secret in any form."""
    ref = seal(world, store)
    header, body = store.get_object(ref["object_id"], ref["revision_id"])
    token = SECRET["access_token"].encode()
    assert token not in body
    assert not any(token[i:i + 12] in body for i in range(0, len(token) - 12))


# ── One object, many revisions ────────────────────────────────────────────


def test_writing_the_same_setting_twice_gives_two_revisions_of_one_object(
    world, store
):
    first = seal(world, store, setting_id="row-1")
    second = seal(world, store, setting_id="row-2", payload={"access_token": "second"})
    assert first["object_id"] == second["object_id"], "revisions must share an object"
    assert first["revision_id"] != second["revision_id"]
    # Both remain independently readable -- an append-only history.
    opened = [
        open_setting(r, held_secrets={world.s1.state_id: world.sec1},
                     bridges=world.bridges, descriptors=world.descriptors, store=store)
        for r in (first, second)
    ]
    assert opened[0] == SECRET
    assert opened[1] == {"access_token": "second"}


def test_identifiers_are_derived_not_stored(world):
    """Two nodes must agree on them without coordinating."""
    g = world.sim.genesis_id
    assert object_id_for(g, SET_ID, KEY) == object_id_for(g, SET_ID, KEY)
    assert object_id_for(g, SET_ID, KEY) != object_id_for(g, SET_ID, "other-key")
    assert object_id_for(g, SET_ID, KEY) != object_id_for("b" * 64, SET_ID, KEY)
    assert revision_id_for(g, "row-1") != revision_id_for(g, "row-2")
    # And the two derivations can never collide with each other.
    assert object_id_for(g, SET_ID, KEY) != revision_id_for(g, SET_ID)


# ── A reader without the keys gets a refusal, never plaintext ─────────────


def test_a_reader_holding_nothing_is_refused(world, store):
    ref = seal(world, store)
    with pytest.raises(Exception) as exc:
        open_setting(ref, held_secrets={}, bridges=world.bridges,
                     descriptors=world.descriptors, store=store)
    assert SECRET["access_token"] not in str(exc.value)


def test_a_reader_holding_only_an_older_key_is_refused(world, store):
    """After keys move on, whoever kept the old one cannot read new writes."""
    ref = seal(world, store)
    with pytest.raises(Exception):
        open_setting(ref, held_secrets={world.s0.state_id: world.sec0},
                     bridges=world.bridges, descriptors=world.descriptors, store=store)


# ── The reference itself is strict ────────────────────────────────────────


def test_a_reference_with_an_extra_field_is_refused():
    ref = build_reference(
        genesis_id="a" * 64, domain_id="d", object_id="o", revision_id="r",
        storage_state_id="s", policy_class="password",
    )
    with pytest.raises(VaultError, match="exactly"):
        parse_reference({**ref, "smuggled": "x"})


def test_a_policy_class_this_build_cannot_open_is_refused():
    """Storing under a class nothing can open would strand the secret."""
    with pytest.raises(VaultError, match="unknown policy class"):
        build_reference(
            genesis_id="a" * 64, domain_id="d", object_id="o", revision_id="r",
            storage_state_id="s", policy_class="prf",
        )
    ref = build_reference(
        genesis_id="a" * 64, domain_id="d", object_id="o", revision_id="r",
        storage_state_id="s", policy_class="password",
    )
    with pytest.raises(VaultError, match="unknown policy class"):
        parse_reference({**ref, "policy_class": "both"})


def test_an_ordinary_settings_payload_is_not_mistaken_for_a_reference():
    assert not is_vault_reference({"interval": "quarterly"})
    assert not is_vault_reference({"vault": "something-else"})
    assert not is_vault_reference(None)
    with pytest.raises(VaultError, match="not a vault reference"):
        parse_reference({"interval": "quarterly"})
