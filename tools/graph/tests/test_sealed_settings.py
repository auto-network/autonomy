"""Unit tests for the SealedSettings crypto core + transparent surface.

These drive the layer through a fake in-memory backend, so they exercise the
key derivation, blind indexing, hidden (seal-all) addressing, AEAD sealing,
lazy get-or-create, and fail-closed listing WITHOUT any dashboard or database.
The ClientBackend adapter is runtime-critical and is validated separately on a
live run.
"""

from __future__ import annotations

import unicodedata

import pytest

from tools.graph.sealed_settings import (
    SealedSettings,
    SealedSettingsError,
    VaultLocked,
    blind_index,
    hidden_address,
)

PEPPER = bytes(range(32))


class FakeBackend:
    """In-memory SealedBackend.

    ``locked`` makes every sealed-index release raise; ``pepper`` is ``None``
    for never-minted and a :class:`VaultLocked` state string in
    ``pepper_locked`` for present-but-unreleasable.
    """

    def __init__(self, pepper: bytes | None = PEPPER):
        self.pepper = pepper
        self.pepper_locked: str | None = None
        self.sealed_indexes: dict[str, bytes] = {}
        self.rows: dict[str, str] = {}
        self.mint_calls = 0
        self.open_calls = 0
        self.locked: str | None = None

    def read_pepper(self):
        if self.pepper_locked is not None:
            raise VaultLocked(self.pepper_locked)
        if self.pepper is None:
            raise SealedSettingsError("the sealed-settings pepper was never minted")
        return self.pepper

    def read_sealed_index(self, address, *, block):
        self.open_calls += 1
        if self.locked is not None:
            raise VaultLocked(self.locked)
        return self.sealed_indexes.get(address)

    def mint_sealed_index(self, address, value_hex):
        self.mint_calls += 1
        self.sealed_indexes.setdefault(address, bytes.fromhex(value_hex))

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


def test_nothing_stored_names_the_store_or_item():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"secret_hint": "x"})
    tag = hidden_address(PEPPER, "pm")
    (row_key,) = list(backend.rows)
    assert row_key.startswith(f"{tag}:")
    # Seal-all: no key in either set opens with the plaintext store name or
    # carries the item name or the scheme's own label. (Substring checks are
    # limited to strings long enough not to occur in base64 by chance.)
    for stored_key in list(backend.rows) + list(backend.sealed_indexes):
        assert not stored_key.startswith("pm:")
        assert "Chase" not in stored_key
        assert "sealed-settings" not in stored_key
    (ciphertext,) = list(backend.rows.values())
    assert "Chase" not in ciphertext and "chase.com" not in ciphertext


def test_sealed_index_lives_at_the_hidden_address():
    backend = FakeBackend()
    SealedSettings("pm", backend).put("a", {"v": 1})
    assert set(backend.sealed_indexes) == {hidden_address(PEPPER, "pm")}


def test_addresses_are_pepper_dependent():
    assert hidden_address(PEPPER, "pm") != hidden_address(bytes(32), "pm")
    a = FakeBackend()
    b = FakeBackend(pepper=bytes(reversed(range(32))))
    SealedSettings("pm", a).put("x", {"v": 1})
    SealedSettings("pm", b).put("x", {"v": 1})
    assert set(a.sealed_indexes) != set(b.sealed_indexes)
    assert set(a.rows) != set(b.rows)


def test_pepper_never_minted_is_a_clear_error():
    with pytest.raises(SealedSettingsError, match="pepper"):
        SealedSettings("pm", FakeBackend(pepper=None)).get("anything")


def test_pepper_cold_raises_typed_lock():
    backend = FakeBackend()
    backend.pepper_locked = VaultLocked.COLD
    with pytest.raises(VaultLocked) as exc:
        SealedSettings("pm", backend).get("anything")
    assert exc.value.state == VaultLocked.COLD
    assert backend.mint_calls == 0


def test_list_returns_names_and_metadata():
    store = SealedSettings("notes", FakeBackend())
    store.put("groceries", {"body": "milk"})
    store.put("todo", {"body": "ship it"})
    got = {item.name: item.metadata for item in store.list()}
    assert got == {"groceries": {"body": "milk"}, "todo": {"body": "ship it"}}


def test_sealed_index_minted_once_then_cached():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("a", {"v": 1})
    store.put("b", {"v": 2})
    store.get("a")
    assert backend.mint_calls == 1
    # Two release calls at first unlock (miss -> mint -> hit), none after.
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


def test_different_sealed_index_cannot_read_rows():
    backend = FakeBackend()
    SealedSettings("pm", backend).put("Chase", {"u": "me"})
    # A second store sharing the pepper (same tag, same rows) but forced onto
    # a different sealed index sees none of the rows.
    other = FakeBackend()
    other.rows = backend.rows        # same storage, same hidden tag...
    other.sealed_indexes = {}        # ...but its own sealed index gets minted
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
    tag = hidden_address(PEPPER, "pm")
    a_key = f"{tag}:{blind_index(k_index, 'A')}"
    b_key = f"{tag}:{blind_index(k_index, 'B')}"
    # Move A's blob into B's row: the AAD (row key) no longer matches -> skipped.
    backend.rows[b_key] = backend.rows[a_key]
    assert {i.name for i in store.list()} == {"A"}


def test_blind_index_is_deterministic_and_matches_row_key():
    store = SealedSettings("pm", FakeBackend())
    store.put("Chase", {"v": 1})  # forces the sealed index
    k_index = store._keys()[0]
    tag = hidden_address(PEPPER, "pm")
    assert store.blind_index("Chase") == f"{tag}:{blind_index(k_index, 'Chase')}"
    assert blind_index(k_index, "Chase") == blind_index(k_index, "Chase")


def test_hidden_address_is_opaque_base64url():
    tag = hidden_address(PEPPER, "pm")
    assert tag == hidden_address(PEPPER, "pm")
    assert ":" not in tag and "=" not in tag
    assert tag != hidden_address(PEPPER, "notes")


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


def test_client_backend_resolves_org_prefixed_sealed_index():
    """The vault-settings seam stores session mints org-prefixed, and the
    vault_open rendezvous takes the BARE suffix (the server derives the org
    prefix). ClientBackend must detect the prefixed row, open with the bare
    address, refuse an operator-only (unprefixed) store loudly, and stay
    silent for a store never minted."""
    from types import SimpleNamespace

    from tools.graph.sealed_settings import ClientBackend

    address = "gpZ0yFl8HO0uX63yAtFOtyMxp3HEgXrO1onEcTRUhCk"

    class StubClient:
        def __init__(self, keys):
            self._members = [
                SimpleNamespace(key=k, payload=None, vault_error=None)
                for k in keys
            ]
            self.opened_with = None

        def read_set(self, set_id, *, org):
            assert org is None  # bearer-derived scope, never "personal"
            return SimpleNamespace(members=self._members)

        def request_vault_open(self, set_id, key, *, org, ttl_seconds):
            self.opened_with = key
            import tempfile, os
            fd, path = tempfile.mkstemp()
            os.write(fd, (b"ab" * 32))
            os.close(fd)
            return {"path": path}

    # Session-minted (prefixed) store: open with the BARE address.
    stub = StubClient(["autonomy:" + address, "autonomy:mac.ssh"])
    got = ClientBackend(stub).read_sealed_index(address, block=False)
    assert stub.opened_with == address
    assert got == bytes.fromhex("ab" * 32)

    # Operator-minted (unprefixed) store: unreachable from a session — fail
    # loud rather than mint a shadow index beside it.
    stub = StubClient([address])
    with pytest.raises(SealedSettingsError, match="operator"):
        ClientBackend(stub).read_sealed_index(address, block=False)
    assert stub.opened_with is None

    # Absent stays absent — no ceremony for a store never minted.
    stub = StubClient(["autonomy:mac.ssh"])
    assert ClientBackend(stub).read_sealed_index(address, block=False) is None
    assert stub.opened_with is None
