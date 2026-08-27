"""The policy-class chooser's passkey list: ONE entry per credential.

An iCloud passkey is one logical key no matter how many machines hold PRF
slots for it (operator ruling 2026-08-27): the chooser must group the vault's
per-device passkey factors by their WebAuthn credential and offer each
credential once, carrying every member factor underneath. Passwords never
appear; a vault factor with no known credential linkage is listed honestly on
its own rather than hidden.
"""

from __future__ import annotations

import pytest

from tools.vault.service import unique_passkey_choices
from tools.vault.store import VaultStore


def memory_store() -> VaultStore:
    return VaultStore(":memory:")


def _put_passkey(store, factor_id: str, public_key: str) -> None:
    store.put_passkey_factor(factor_id, public_key)


def test_one_entry_per_credential_grouping_every_device_slot():
    store = memory_store()
    # credential X synced to two machines → two vault factors, one logical key
    _put_passkey(store, "pk.x.mac", "a" * 64)
    _put_passkey(store, "pk.x.iphone", "b" * 64)
    # credential Y on one machine
    _put_passkey(store, "pk.y", "c" * 64)
    # a password factor must never appear in the passkey chooser
    store.put_password_factor("pw.main", "d" * 64, "armor-blob")

    rows = [
        {"credential_id": "credX", "label": "iCloud Passkey",
         "provisioning_public_keys": ["a" * 64, "b" * 64]},
        {"credential_id": "credY", "label": "YubiKey 5C",
         "provisioning_public_keys": ["c" * 64]},
    ]
    choices = unique_passkey_choices(store, rows)

    assert [c["credential_id"] for c in choices] == ["credX", "credY"]
    x = choices[0]
    assert x["label"] == "iCloud Passkey"
    assert x["factor_ids"] == ["pk.x.iphone", "pk.x.mac"], "every device slot, one entry"
    assert x["public_keys"] == ["b" * 64, "a" * 64]  # keys stay paired with factor ids
    y = choices[1]
    assert y["factor_ids"] == ["pk.y"]
    assert all("pw" not in fid for c in choices for fid in c["factor_ids"])


def test_single_key_row_shape_is_accepted():
    store = memory_store()
    _put_passkey(store, "pk.y", "c" * 64)
    # the identity layer's CURRENT row shape: one provisioning_public_key
    choices = unique_passkey_choices(store, [
        {"credential_id": "credY", "label": "YubiKey 5C",
         "provisioning_public_key": "c" * 64},
    ])
    assert len(choices) == 1
    assert choices[0]["credential_id"] == "credY"
    assert choices[0]["factor_ids"] == ["pk.y"]


def test_unlinked_factors_are_listed_honestly_not_hidden():
    store = memory_store()
    _put_passkey(store, "pk.orphan", "e" * 64)
    choices = unique_passkey_choices(store, [])
    assert len(choices) == 1
    assert choices[0]["credential_id"] is None
    assert choices[0]["factor_ids"] == ["pk.orphan"]
    assert choices[0]["label"] == "pk.orphan"


def test_credentials_with_no_enrolled_vault_factor_are_omitted():
    store = memory_store()
    choices = unique_passkey_choices(store, [
        {"credential_id": "credZ", "label": "Never enrolled",
         "provisioning_public_keys": ["f" * 64]},
    ])
    assert choices == []
