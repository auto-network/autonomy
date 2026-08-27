"""Unit tests for the SealedSettings crypto core + transparent surface.

These drive the layer through a fake in-memory backend, so they exercise the
key derivation, blind indexing, AEAD sealing, lazy get-or-create, and
fail-closed listing WITHOUT any dashboard or database. The ClientBackend
adapter is runtime-critical and is validated separately on a live run.
"""

from __future__ import annotations

import unicodedata

import pytest

from tools.graph.sealed_settings import (
    SealedSettings,
    VaultLocked,
    blind_index,
)


class FakeBackend:
    """In-memory SealedBackend. ``locked`` makes every root release raise."""

    def __init__(self):
        self.roots: dict[str, bytes] = {}
        self.rows: dict[str, str] = {}
        self.mint_calls = 0
        self.open_calls = 0
        self.locked: str | None = None

    def read_root(self, secured_key, *, block):
        self.open_calls += 1
        if self.locked is not None:
            raise VaultLocked(self.locked)
        return self.roots.get(secured_key)

    def mint_root(self, secured_key, value_hex):
        self.mint_calls += 1
        self.roots.setdefault(secured_key, bytes.fromhex(value_hex))

    def read_row(self, row_key):
        return self.rows.get(row_key)

    def write_row(self, row_key, ciphertext):
        self.rows[row_key] = ciphertext

    def delete_row(self, row_key):
        self.rows.pop(row_key, None)

    def list_rows(self, prefix):
        for key, value in self.rows.items():
            if key.startswith(f"{prefix}:"):
                yield key, value


def test_put_get_roundtrip():
    store = SealedSettings("pm", FakeBackend())
    store.put("Chase", {"username": "me", "url": "chase.com"})
    assert store.get("Chase") == {"username": "me", "url": "chase.com"}


def test_get_absent_is_none():
    assert SealedSettings("pm", FakeBackend()).get("nope") is None


def test_key_is_blind_indexed_never_plaintext():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"secret_hint": "x"})
    (row_key,) = list(backend.rows)
    assert row_key.startswith("pm:")
    assert "Chase" not in row_key
    # And the stored value carries no plaintext name/metadata either.
    (ciphertext,) = list(backend.rows.values())
    assert "Chase" not in ciphertext and "chase.com" not in ciphertext


def test_list_returns_names_and_metadata():
    store = SealedSettings("notes", FakeBackend())
    store.put("groceries", {"body": "milk"})
    store.put("todo", {"body": "ship it"})
    got = {item.name: item.metadata for item in store.list()}
    assert got == {"groceries": {"body": "milk"}, "todo": {"body": "ship it"}}


def test_root_minted_once_then_cached():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("a", {"v": 1})
    store.put("b", {"v": 2})
    store.get("a")
    assert backend.mint_calls == 1
    # Two read_root calls at first unlock (miss -> mint -> hit), none after.
    assert backend.open_calls == 2


def test_domains_are_isolated():
    backend = FakeBackend()
    pm = SealedSettings("pm", backend)
    notes = SealedSettings("notes", backend)
    pm.put("shared-name", {"kind": "password"})
    notes.put("shared-name", {"kind": "note"})
    assert pm.get("shared-name") == {"kind": "password"}
    assert notes.get("shared-name") == {"kind": "note"}
    assert {i.name for i in pm.list()} == {"shared-name"}
    assert len(list(backend.rows)) == 2  # same logical name, two distinct rows


def test_different_root_cannot_read_rows():
    backend = FakeBackend()
    SealedSettings("pm", backend).put("Chase", {"u": "me"})
    # A second store forced onto a different root sees none of the rows.
    other = FakeBackend()
    other.rows = backend.rows  # same storage...
    other.roots = {}           # ...but its own (different) root will be minted
    assert SealedSettings("pm", other).list() == []


def test_locked_vault_raises_typed_error():
    backend = FakeBackend()
    backend.locked = VaultLocked.PENDING
    with pytest.raises(VaultLocked) as exc:
        SealedSettings("pm", backend).get("anything")
    assert exc.value.state == VaultLocked.PENDING
    assert backend.mint_calls == 0  # never mint while locked


def test_tampered_row_is_skipped_not_surfaced():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("good", {"v": 1})
    store.put("bad", {"v": 2})
    # Corrupt one row's ciphertext; it must vanish from list(), not leak.
    bad_key = store.blind_index("bad")
    backend.rows[bad_key] = backend.rows[bad_key][:-4] + "AAAA"
    names = {i.name for i in store.list()}
    assert names == {"good"}


def test_aad_binds_ciphertext_to_its_row():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("A", {"v": "a"})
    store.put("B", {"v": "b"})
    k_index = store._keys()[0]
    a_key = f"pm:{blind_index(k_index, 'A')}"
    b_key = f"pm:{blind_index(k_index, 'B')}"
    # Move A's blob into B's row: the AAD (row key) no longer matches -> skipped.
    backend.rows[b_key] = backend.rows[a_key]
    assert {i.name for i in store.list()} == {"A"}


def test_blind_index_is_deterministic_and_matches_row_key():
    store = SealedSettings("pm", FakeBackend())
    store.put("Chase", {"v": 1})  # forces the root
    k_index = store._keys()[0]
    assert store.blind_index("Chase") == f"pm:{blind_index(k_index, 'Chase')}"
    assert blind_index(k_index, "Chase") == blind_index(k_index, "Chase")


def test_domain_with_colon_rejected():
    with pytest.raises(ValueError):
        SealedSettings("bad:domain", FakeBackend())


def test_unicode_names_normalized():
    store = SealedSettings("pm", FakeBackend())
    # 'e' + combining acute (NFD) vs the precomposed U+00E9 (NFC).
    decomposed = unicodedata.normalize("NFD", "cafe\u0301")
    precomposed = unicodedata.normalize("NFC", "cafe\u0301")
    assert precomposed != decomposed
    store.put(precomposed, {"v": 1})
    # The other normalization form addresses the SAME row.
    assert store.get(decomposed) == {"v": 1}
    assert len(list(store.list())) == 1
