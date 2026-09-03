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
        self.concurrent_winner: dict[str, str] = {}
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
        from tools.graph.sealed_settings import _bare_key
        for k, v in self.rows.items():
            if _bare_key(k) == row_key:
                return v
        return None

    def write_row(self, row_key, ciphertext):
        # A concurrent winner may already occupy this key. The fake mirrors
        # the substrate: last write is stored, and the AUTHORITATIVE (stored)
        # ciphertext is returned so the caller can consume the write-result.
        winner = self.concurrent_winner.pop(row_key, None)
        if winner is not None:
            self.rows[row_key] = winner
        else:
            self.rows[row_key] = ciphertext
        return self.rows[row_key]

    def delete_row(self, row_key):
        self.rows.pop(row_key, None)

    def list_rows(self, prefix):
        from tools.graph.sealed_settings import _bare_key
        for key, value in self.rows.items():
            if _bare_key(key).startswith(f"{prefix}."):
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
    assert row_key.startswith(f"{tag}.")
    # Seal-all: no key in either set opens with the plaintext store name or
    # carries the item name or the scheme's own label. (Substring checks are
    # limited to strings long enough not to occur in base64 by chance.)
    for stored_key in list(backend.rows) + list(backend.sealed_indexes):
        assert not stored_key.startswith("pm.") and not stored_key.startswith("pm:")
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
    a_key = f"{tag}.{blind_index(k_index, 'A')}"
    b_key = f"{tag}.{blind_index(k_index, 'B')}"
    # Move A's blob into B's row: the AAD (row key) no longer matches -> skipped.
    backend.rows[b_key] = backend.rows[a_key]
    assert {i.name for i in store.list()} == {"A"}


def test_blind_index_is_deterministic_and_matches_row_key():
    store = SealedSettings("pm", FakeBackend())
    store.put("Chase", {"v": 1})  # forces the sealed index
    k_index = store._keys()[0]
    tag = hidden_address(PEPPER, "pm")
    assert store.blind_index("Chase") == f"{tag}.{blind_index(k_index, 'Chase')}"
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


def test_client_backend_resolves_org_prefixed_sealed_index(monkeypatch):
    """The vault-settings seam stores session mints org-prefixed, and the
    vault_open rendezvous takes the BARE suffix (the server derives the org
    prefix). ClientBackend must detect the prefixed row, open with the bare
    address, refuse an operator-only (unprefixed) store loudly, and stay
    silent for a store never minted."""
    from types import SimpleNamespace

    from tools.graph.sealed_settings import ClientBackend

    # This test exercises the request path; neutralize the /run/secrets reuse
    # so a real released index on the host cannot short-circuit it.
    monkeypatch.setattr(ClientBackend, "_released_path", staticmethod(lambda _a: None))

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


def test_cold_is_read_from_the_structured_reason_not_the_error_prose(monkeypatch):
    """Cold must be decided by VaultReadFailure.reason, never by sniffing text.

    The removed ``_looks_cold`` matched any message containing "no key", so an
    UNRELATED failure was reported as a cold vault — the false positive that
    sent an investigation after a lock that was not there. A genuine
    ``no_key_holder`` reason still raises COLD; look-alike prose must not.
    """
    from types import SimpleNamespace

    from tools.graph.client import GraphHttpError
    from tools.graph.sealed_settings import ClientBackend
    from tools.graph.settings_ops import VAULT_NO_KEY_HOLDER

    monkeypatch.setattr(
        ClientBackend, "_released_path", staticmethod(lambda _a: None))
    address = "gpZ0yFl8HO0uX63yAtFOtyMxp3HEgXrO1onEcTRUhCk"
    key = "autonomy:" + address

    class StubClient:
        def __init__(self, *, vault_error=None, raises=None):
            self._member = SimpleNamespace(
                key=key, payload=None, vault_error=vault_error)
            self._raises = raises

        def read_set(self, set_id, *, org):
            return SimpleNamespace(members=[self._member])

        def request_vault_open(self, set_id, k, *, org, ttl_seconds):
            raise self._raises

    # The structured reason IS the signal.
    cold = StubClient(
        vault_error=SimpleNamespace(reason=VAULT_NO_KEY_HOLDER, message="…"))
    with pytest.raises(VaultLocked) as exc:
        ClientBackend(cold).read_sealed_index(address, block=False)
    assert exc.value.state == VaultLocked.COLD

    # A DIFFERENT structured reason is not cold — it must not be swallowed as
    # a lock state just because the prose is vault-flavoured.
    other = StubClient(
        vault_error=SimpleNamespace(
            reason="policy_mismatch",
            message="the vault is locked out of this policy class"))
    with pytest.raises(SealedSettingsError) as exc:
        ClientBackend(other).read_sealed_index(address, block=False)
    assert not isinstance(exc.value, VaultLocked)

    # THE REGRESSION: an unrelated error whose text happens to contain
    # "no key" used to be classified COLD. It must now surface as itself.
    noisy = StubClient(
        raises=GraphHttpError("found no key at that address", 500, {}))
    with pytest.raises(GraphHttpError):
        ClientBackend(noisy).read_sealed_index(address, block=False)


def test_layer_reads_back_server_prefixed_rows():
    """A session's item rows are stored under a server-derived <org>: prefix.
    The layer writes bare keys but must resolve get/list against the prefixed
    stored keys, and the seal's AAD (the bare key) must still verify."""
    from tools.graph.sealed_settings import hidden_address

    class PrefixingBackend(FakeBackend):
        """Simulates the org-writeback seam: every written row key is stored
        with an 'org:' prefix, exactly as the server derives it."""

        def write_row(self, row_key, ciphertext):
            self.rows["org:" + row_key] = ciphertext
            return ciphertext

    backend = PrefixingBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"u": "me"})
    store.put("Amex", {"u": "you"})

    tag = hidden_address(PEPPER, "pm")
    assert all(k.startswith(f"org:{tag}.") for k in backend.rows)

    # get() (bare lookup) resolves the prefixed row and its AAD verifies.
    assert store.get("Chase") == {"u": "me"}
    # list() opens both prefixed rows.
    assert {i.name: i.metadata for i in store.list()} == {
        "Chase": {"u": "me"}, "Amex": {"u": "you"}}


def test_bare_key_strips_optional_org_prefix():
    from tools.graph.sealed_settings import _bare_key

    assert _bare_key("tag.blind") == "tag.blind"
    assert _bare_key("autonomy:tag.blind") == "tag.blind"


def test_client_backend_reuses_released_sealed_index_without_reapproval(monkeypatch):
    """An already-released sealed index (at /run/secrets/<name> from a prior
    approval this session) is reused directly — no second vault_open."""
    from types import SimpleNamespace

    from tools.graph.sealed_settings import ClientBackend

    address = "gpZ0yFl8HO0uX63yAtFOtyMxp3HEgXrO1onEcTRUhCk"

    class StubClient:
        def __init__(self):
            self.opened = False

        def read_set(self, set_id, *, org):
            return SimpleNamespace(members=[
                SimpleNamespace(key="autonomy:" + address,
                                payload=None, vault_error=None)])

        def request_vault_open(self, *a, **k):
            self.opened = True
            raise AssertionError("must not re-request an already-released index")

    stub = StubClient()
    monkeypatch.setattr(ClientBackend, "_released_path",
                        staticmethod(lambda addr: bytes(32) if addr == address else None))
    got = ClientBackend(stub).read_sealed_index(address, block=False)
    assert got == bytes(32)
    assert stub.opened is False


def test_put_returns_won_outcome_uncontended():
    from tools.graph.sealed_settings import WriteOutcome

    store = SealedSettings("pm", FakeBackend())
    outcome = store.put("Chase", {"u": "me"})
    assert isinstance(outcome, WriteOutcome)
    assert outcome.won is True
    assert outcome.value == {"u": "me"}


def test_put_consumes_authoritative_value_when_it_loses():
    """A concurrent writer's row is authoritative: put() reports won=False and
    returns THAT value, so the loser converges without any timestamp logic."""
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"u": "me"})  # establish the store + a first value

    # Arrange a concurrent winner for the next write: seal a rival value under
    # the same k_meta/row so it decrypts, and make the backend resolve to it.
    from tools.graph.sealed_settings import _seal
    k_meta = store._keys()[1]
    row_key = store._row_key("Chase")
    rival_ct = _seal(k_meta, __import__("json").dumps(
        {"name": "Chase", "metadata": {"u": "rival"}},
        sort_keys=True, separators=(",", ":")).encode(), row_key.encode())
    backend.concurrent_winner[row_key] = rival_ct

    outcome = store.put("Chase", {"u": "mine"})
    assert outcome.won is False
    assert outcome.value == {"u": "rival"}          # converged on the winner
    assert store.get("Chase") == {"u": "rival"}     # and a read agrees


def test_put_never_compares_timestamps():
    """The outcome is derived from the returned authoritative ciphertext, so a
    backend that provides no time ordering still yields a definite result."""
    store = SealedSettings("notes", FakeBackend())
    a = store.put("n", {"v": 1})
    b = store.put("n", {"v": 2})  # overwrite
    assert a.won and b.won
    assert b.value == {"v": 2}
    assert store.get("n") == {"v": 2}


def test_share_and_discover_roundtrip():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"u": "me"})
    store.put("Amex", {"u": "you"})
    store.put("Private", {"u": "secret"})

    store.share("Chase", "family")
    store.share("Amex", "family")

    listing = store.discover("family")
    names = {i.name for i in listing.items}
    assert names == {"Chase", "Amex"}          # exactly the shared items
    assert listing.pending == []
    # Metadata comes through, opened via the item's own seal.
    got = {i.name: i.metadata for i in listing.items}
    assert got["Chase"] == {"u": "me"}
    # An unshared item is not discoverable.
    assert "Private" not in names
    # An empty audience discovers nothing.
    assert store.discover("nobody").items == []


def test_share_is_idempotent():
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"u": "me"})
    store.share("Chase", "family")
    store.share("Chase", "family")   # re-share must not duplicate
    listing = store.discover("family")
    assert [i.name for i in listing.items] == ["Chase"]


def test_membership_rows_do_not_correlate_with_item_rows():
    """Approach B: a cold reader must not be able to join a membership row to
    the item row it references, nor to the store — the member address is a
    separate HMAC and the reference value is sealed."""
    import json as _json
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"u": "me", "url": "chase.com"})
    store.share("Chase", "family")

    item_key = store._row_key("Chase")            # <tag>.<blind-index>
    item_suffix = item_key.split(".", 1)[1]       # the item's blind index
    member_key = store._member_key("family", "Chase")
    # The membership row's address shares NO component with the item row.
    assert item_suffix not in member_key
    assert not member_key.startswith(store._store_tag())
    # The membership VALUE is ciphertext, not the plaintext item key: the
    # reference to the item never appears in clear in the membership row.
    member_value = backend.rows[member_key]
    assert item_key not in member_value
    assert "chase.com" not in member_value and "Chase" not in member_value
    # And no row's VALUE leaks the item name or a field value in clear.
    for v in backend.rows.values():
        assert "chase.com" not in v and "Chase" not in v


def test_discover_reports_pending_for_unreplicated_rows():
    """A membership referencing an item whose row is absent is PENDING, not
    dropped and not conflated with 'no such item'."""
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"u": "me"})
    store.share("Chase", "family")
    # Simulate the item row not having replicated to this reader: drop it,
    # keep the membership.
    del backend.rows[store._row_key("Chase")]
    listing = store.discover("family")
    assert listing.items == []
    assert listing.pending == [store._row_key("Chase")]


def test_forged_membership_fails_closed():
    """A membership whose sealed reference cannot be opened (foreign key) is
    dropped — a forged hint grants nothing."""
    backend = FakeBackend()
    store = SealedSettings("pm", backend)
    store.put("Chase", {"u": "me"})
    store.share("Chase", "family")
    # Corrupt the membership ciphertext: discovery must skip it, not crash.
    mk = store._member_key("family", "Chase")
    # keys are stored bare in FakeBackend
    backend.rows[mk] = backend.rows[mk][:-4] + "AAAA"
    listing = store.discover("family")
    assert listing.items == [] and listing.pending == []


def test_client_backend_pepper_get_or_create_by_suffix(monkeypatch):
    """Per-org pepper: read matches by suffix (the row is stored <org>:PEPPER_KEY
    for an org session), and mints get-or-create when the caller's scope has none."""
    from types import SimpleNamespace
    from tools.graph.sealed_settings import ClientBackend, PEPPER_KEY

    class StubClient:
        def __init__(self):
            self.rows = {}          # key -> value hex
            self.minted = 0

        def read_set(self, set_id, *, org):
            members = [SimpleNamespace(key=k, payload={"value": v}, vault_error=None)
                       for k, v in self.rows.items()]
            return SimpleNamespace(members=members)

        def add_setting(self, set_id, rev, key, payload, *, state, org):
            # Simulate the server deriving the caller's <org>: prefix on write.
            self.minted += 1
            self.rows["autonomy:" + key] = payload["value"]

    stub = StubClient()
    cb = ClientBackend(stub)
    # First read: absent -> mints (get-or-create) -> returns 32 bytes.
    p1 = cb.read_pepper()
    assert len(p1) == 32 and stub.minted == 1
    # Row was stored org-prefixed; a second read finds it by SUFFIX, no re-mint.
    assert list(stub.rows) == ["autonomy:" + PEPPER_KEY]
    p2 = cb.read_pepper()
    assert p2 == p1 and stub.minted == 1  # never rotates


def test_ops_backend_in_process_roundtrip(monkeypatch):
    """OpsBackend drives SealedSettings in-process via settings_ops: audited
    pepper get-or-create, sealed-row put/get/list org-scoped, with the store's
    sealed index injected (opening it is the ceremony's job)."""
    import types
    from tools.graph import sealed_settings as ss
    from tools.graph.sealed_settings import OpsBackend, SealedSettings, PEPPER_KEY

    # A tiny fake settings_ops: audited + sealed-row sets as dicts, with the
    # server deriving the <org>: writeback prefix on write_by_key.
    class FakeOps:
        VAULT = "autonomy.vault.audited"
        ROWS = "autonomy.sealed-settings.row"
        def __init__(self):
            self.store = {self.VAULT: {}, self.ROWS: {}}
        def read_set(self, set_id, *, org, peers=None):
            from types import SimpleNamespace
            members = [SimpleNamespace(key=k, payload={"value": v} if set_id==self.VAULT
                                       else {"ciphertext": v}, vault_error=None)
                       for k, v in self.store[set_id].items()]
            return SimpleNamespace(members=members)
        def write_by_key(self, set_id, rev, key, payload, *, org, state):
            derived = f"{org}:{key}" if org else key   # server derives prefix
            self.store[set_id][derived] = payload.get("value") or payload.get("ciphertext")
    fake = FakeOps()
    monkeypatch.setattr(OpsBackend, "_ops", lambda self: fake)

    idx = bytes(range(32))
    store = SealedSettings("notes", OpsBackend("autonomy", sealed_index=idx))
    store.put("n1", {"title": "hello"})
    store.put("n2", {"title": "world"})
    assert store.get("n1") == {"title": "hello"}
    assert {i.name: i.metadata for i in store.list()} == {
        "n1": {"title": "hello"}, "n2": {"title": "world"}}
    # pepper was get-or-created under the org writeback prefix
    assert any(k.startswith("autonomy:" + PEPPER_KEY) for k in fake.store[FakeOps.VAULT])
    # item rows are stored org-prefixed too
    assert all(k.startswith("autonomy:") for k in fake.store[FakeOps.ROWS])
