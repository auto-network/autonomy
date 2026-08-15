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
    build_locator,
    is_vault_locator,
    object_id_for,
    open_setting,
    parse_locator,
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


def seal_other_setting(world, store, setting_id):
    """A different setting entirely -- so its object id differs."""
    return seal_setting(
        author=world.member, frontier=world.f1, setting_id=setting_id,
        set_id="dashboard.other.credentials", key=KEY, payload={"x": 1},
        held_secrets={world.s1.state_id: world.sec1},
        available_states={world.s1.state_id: world.s1},
        ancestry=world.ancestry, store=store,
        bridges=world.bridges, descriptors=world.descriptors,
    )


# ── The row holds a locator, never the secret ─────────────────────────────


def test_the_settings_row_holds_no_ciphertext_and_no_secret(world, store):
    locator = seal(world, store)
    assert SECRET["access_token"] not in locator
    assert SECRET["refresh"] not in locator
    assert is_vault_locator(locator)
    assert set(parse_locator(locator)) == {
        "genesis_id", "domain_id", "object_id", "revision_id",
        "storage_state_id", "tier",
    }


def test_the_locator_is_an_opaque_scalar(world, store):
    """Design of record §17: the locator MUST be a scalar, not an object.

    Settings resolution merge-patches candidate rows (RFC 7386) BEFORE
    anything is decrypted, and merge-patch recurses into objects. An
    object-shaped locator would be merged field by field.
    """
    locator = seal(world, store)
    assert isinstance(locator, str)


def test_a_partial_override_cannot_splice_two_locators(world, store):
    """The failure §17 names: a wrap key for an object never written.

    Two writes produce two locators. Merge-patching one payload over the other
    must replace the locator WHOLE. If it were an object, RFC 7386 would merge
    per-field and could pair one write's object id with another's revision id.
    """
    first = seal(world, store, setting_id="row-1")
    second = seal(world, store, setting_id="row-2", payload={"access_token": "second"})
    assert parse_locator(first)["revision_id"] != parse_locator(second)["revision_id"]

    # The RESOLVER'S OWN merge, imported rather than reimplemented: the
    # property must hold for the function settings resolution actually calls,
    # so this cannot drift away from it.
    from tools.graph.settings_ops import json_merge_patch

    merged = json_merge_patch({"value": first}, {"value": second})
    # The winner is one locator, entire -- never a blend of the two.
    assert merged["value"] == second
    assert merged["value"] in (first, second)

    # And the defect was REAL, shown with the same function. Two DIFFERENT
    # settings, so the locators differ in more than one field; had they stayed
    # objects, a partial override merges per field and yields one carrying the
    # object id of one and the revision id of the other -- addressing an object
    # that was never written.
    a = parse_locator(seal(world, store, setting_id="row-a"))
    b = parse_locator(
        seal_other_setting(world, store, setting_id="row-b")
    )
    assert a["object_id"] != b["object_id"], "two settings, two objects"

    spliced = json_merge_patch(
        {"value": dict(a)}, {"value": {"revision_id": b["revision_id"]}}
    )["value"]
    assert spliced["object_id"] == a["object_id"]
    assert spliced["revision_id"] == b["revision_id"]
    assert spliced != a and spliced != b, (
        "the object form yields a locator belonging to NEITHER write -- which "
        "is precisely the failure the scalar form prevents"
    )


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


def test_the_stored_body_is_ciphertext(world, store):
    """What lands on disk must not contain the secret in any form."""
    ref = parse_locator(seal(world, store))
    _header, body = store.get_object(ref["object_id"], ref["revision_id"])
    token = SECRET["access_token"].encode()
    assert token not in body
    assert not any(token[i:i + 12] in body for i in range(0, len(token) - 12))


# ── One object, many revisions ────────────────────────────────────────────


def test_writing_the_same_setting_twice_gives_two_revisions_of_one_object(
    world, store
):
    first = parse_locator(seal(world, store, setting_id="row-1"))
    second = parse_locator(
        seal(world, store, setting_id="row-2", payload={"access_token": "second"})
    )
    assert first["object_id"] == second["object_id"], "revisions must share an object"
    assert first["revision_id"] != second["revision_id"]
    # Both remain independently readable -- an append-only history.
    opened = [
        open_setting(loc, held_secrets={world.s1.state_id: world.sec1},
                     bridges=world.bridges, descriptors=world.descriptors, store=store)
        for loc in (
            build_locator(**{k: v for k, v in ref.items()}) for ref in (first, second)
        )
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


def test_a_locator_with_an_extra_field_is_refused():
    import base64 as b64

    from tools.graph.vault import LOCATOR_PREFIX

    body = json.dumps({
        "genesis_id": "a" * 64, "domain_id": "d", "object_id": "o",
        "revision_id": "r", "storage_state_id": "s", "tier": "audited",
        "smuggled": "x",
    }).encode()
    tampered = LOCATOR_PREFIX + b64.urlsafe_b64encode(body).decode().rstrip("=")
    with pytest.raises(VaultError, match="exactly"):
        parse_locator(tampered)


def test_a_tier_this_build_cannot_honour_is_refused():
    """`secured` needs the policy-class wrap (auto-39d26), which is not built.

    Accepting it would promise a human factor nothing enforces -- the label
    would assert protection the code does not provide.
    """
    with pytest.raises(VaultError, match="unimplemented tier"):
        build_locator(
            genesis_id="a" * 64, domain_id="d", object_id="o", revision_id="r",
            storage_state_id="s", tier="secured",
        )


def test_the_tier_travels_inside_the_locator():
    """So a sibling field cannot be merged in to downgrade it separately."""
    locator = build_locator(
        genesis_id="a" * 64, domain_id="d", object_id="o", revision_id="r",
        storage_state_id="s",
    )
    assert parse_locator(locator)["tier"] == "audited"
    assert "audited" not in locator, "the tier is encoded, not a readable sibling"


def test_an_ordinary_settings_payload_is_not_mistaken_for_a_locator():
    assert not is_vault_locator({"interval": "quarterly"})
    assert not is_vault_locator("just a string")
    assert not is_vault_locator(None)
    with pytest.raises(VaultError, match="not a vault locator"):
        parse_locator({"interval": "quarterly"})
