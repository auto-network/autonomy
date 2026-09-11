"""org_storage_delegate.signing_key(): the delegate index row's stored slug is
not the delegate's identity.

The index row (NetworkStorageDelegateV1) is personal-homed, fleet-replicated
and keyed by the org's GENESIS; ``organization`` in its payload is whatever
slug the org had on the machine that minted it. After ``graph org rename``
(org_ops.rename_org rewrites only payload.org, never ``organization``), or on
a fleet machine that mounts the same genesis under another slug, prepare()
still finds the row by genesis and reports key_exists, the browser sends
"reuse", and signing_key() returns None at the slug check — so accept()
raises "cannot be opened" on every login and every org vault write is
unauthored. Genesis is already resolved from that org's own ledger and the
public key is cross-checked, so the slug check adds nothing.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tools.dashboard import org_storage_delegate as osd
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import NETWORK_STORAGE_DELEGATE_SET_ID
from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID
from tools.network.idkit import KeyPair

GENESIS = "ab" * 32
FUTURE_MS = int(time.time() * 1000) + 60 * 24 * 3600 * 1000


class _Settings:
    """In-memory stand-in for the Settings reads/writes the module makes."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.hidden: set[tuple[str, str]] = set()   # deprecated/excluded rows

    def put(self, set_id, key, payload):
        self.rows[(set_id, key)] = {"id": f"{set_id}:{key}", "payload": dict(payload)}

    def read_set_key(self, set_id, key, org=None):
        if (set_id, key) in self.hidden:
            return None
        return self.rows.get((set_id, key))

    def override_setting(self, row_id, patch, org=None):
        for row in self.rows.values():
            if row["id"] == row_id:
                row["payload"].update(patch)
                return
        raise KeyError(row_id)

    def add_setting(self, set_id, revision, key, payload, org=None):
        self.put(set_id, key, payload)

    def upsert_by_key(self, set_id, revision, key, payload, org=None):
        self.put(set_id, key, payload)


@pytest.fixture
def settings(monkeypatch, tmp_path):
    fake = _Settings()
    for name in ("read_set_key", "override_setting", "add_setting", "upsert_by_key"):
        monkeypatch.setattr(settings_ops, name, getattr(fake, name))
    ledger = tmp_path / "org.db"
    ledger.write_bytes(b"")
    monkeypatch.setattr(osd, "org_ledger_db_path", lambda org: ledger)
    return fake


class _Ledger:
    genesis_id = GENESIS

    def __init__(self):
        self.events: set[str] = set()

    def add(self, event):
        self.events.add(getattr(event, "event_id", None))

    def __contains__(self, event_id):
        return event_id in self.events

    def ancestry(self, heads):
        return set(self.events)


class _Store:
    """LedgerStore stand-in: one genesis, fixed heads, optional append failure."""

    def __init__(self, heads=("h1",), append_raises=None):
        # accept() deep-copies the ledger and adds the candidate event to it
        # before folding, asks whether an event id is already in it, and
        # walks ancestry for acknowledged replays.
        self.ledger = _Ledger()
        self._heads = list(heads)
        self._append_raises = append_raises
        self.appended = []

    def __call__(self, path):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def heads(self):
        return list(self._heads)

    def append(self, event):
        if self._append_raises:
            raise self._append_raises
        self.appended.append(event)
        return "e" * 64


def _index(org: str, key: KeyPair) -> dict:
    return {"organization": org, "persona_pub": "cd" * 32, "public_key": key.public_hex,
            "key_reference": "storage-delegate." + GENESIS, "expires_at": FUTURE_MS,
            "grant_event_id": "e" * 64}


# ── item: slug in the replicated index is not the delegate's identity ──


def test_signing_key_opens_the_delegate_whatever_slug_the_index_row_recorded(settings, monkeypatch):
    """The index row is personal-homed and fleet-replicated, keyed by GENESIS;
    the slug it stores is whatever this org was called where it was minted.
    After rename_org, or on a fleet machine that mounts the same genesis
    under another slug, the delegate is still this org's delegate."""
    monkeypatch.setattr(osd, "LedgerStore", _Store())
    key = KeyPair.generate()
    settings.put(NETWORK_STORAGE_DELEGATE_SET_ID, GENESIS, _index("old-slug", key))
    settings.put(VAULT_AUDITED_SET_ID, "storage-delegate." + GENESIS, {"value": key.private_hex})

    opened = osd.signing_key("renamed-slug")

    assert opened is not None, "genesis matched and the public key cross-checks; the slug is not identity"
    assert opened.public_hex == key.public_hex


def test_matching_slug_cannot_select_another_genesis(settings, monkeypatch):
    store = _Store()
    store.ledger.genesis_id = "ef" * 32
    monkeypatch.setattr(osd, "LedgerStore", store)
    key = KeyPair.generate()
    settings.put(NETWORK_STORAGE_DELEGATE_SET_ID, GENESIS, _index("same-slug", key))
    settings.put(VAULT_AUDITED_SET_ID, "storage-delegate." + GENESIS, {"value": key.private_hex})
    assert osd.signing_key("same-slug") is None


def test_public_key_mismatch_is_still_refused_after_rename(settings, monkeypatch):
    monkeypatch.setattr(osd, "LedgerStore", _Store())
    settings.put(NETWORK_STORAGE_DELEGATE_SET_ID, GENESIS, _index("old-slug", KeyPair.generate()))
    settings.put(VAULT_AUDITED_SET_ID, "storage-delegate." + GENESIS,
                 {"value": KeyPair.generate().private_hex})
    with pytest.raises(ValueError, match="public index"):
        osd.signing_key("renamed-slug")
