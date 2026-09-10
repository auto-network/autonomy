"""org_storage_delegate.prepare(): presence of the audited key must follow
the Settings lifecycle, not a raw row scan.

``prepare()`` decides ``key_exists`` with a raw ``SELECT 1 FROM settings``
that ignores deprecated / excluded / superseded rows and override rows, so
a hidden key is reported present, the browser says "reuse", and the reuse
then fails with "cannot be opened" on every sign-in — with "new" unreachable
until Settings are hand-edited.
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

    def chain_setting(self, set_id, key, org=None):
        if (set_id, key) in self.hidden:
            return None
        return {"final": "opaque-locator"} if (set_id, key) in self.rows else None

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
    for name in ("read_set_key", "chain_setting", "override_setting", "add_setting", "upsert_by_key"):
        monkeypatch.setattr(settings_ops, name, getattr(fake, name))
    ledger = tmp_path / "org.db"
    ledger.write_bytes(b"")
    monkeypatch.setattr(osd, "org_ledger_db_path", lambda org: ledger)
    return fake


class _Store:
    """LedgerStore stand-in: one genesis, fixed heads, optional append failure."""

    def __init__(self, heads=("h1",), append_raises=None):
        # accept() deep-copies the ledger and adds the candidate event to it
        # before folding; a copy must therefore accept .add().
        self.ledger = SimpleNamespace(genesis_id=GENESIS, add=lambda event: None)
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


# ── item: presence must follow the Settings lifecycle, not a raw row scan ──


def test_prepare_reports_key_exists_false_for_a_lifecycle_hidden_secret(settings, monkeypatch):
    """A deprecated/excluded/superseded audited row is invisible to every
    Settings read the module later relies on (signing_key → read_set_key).
    Reporting it present makes the browser choose 'reuse', which then fails
    with 'cannot be opened' on every sign-in, with 'new' unreachable."""
    monkeypatch.setattr(osd, "LedgerStore", _Store())
    key = KeyPair.generate()
    reference = "storage-delegate." + GENESIS
    settings.put(NETWORK_STORAGE_DELEGATE_SET_ID, GENESIS, _index("org", key))
    settings.put(VAULT_AUDITED_SET_ID, reference, {"value": key.private_hex})
    settings.hidden.add((VAULT_AUDITED_SET_ID, reference))   # e.g. `graph set deprecate`

    # The raw row is still physically in the table.
    class _Conn:
        def execute(self, sql, params):
            assert params == (VAULT_AUDITED_SET_ID, reference)
            return SimpleNamespace(fetchone=lambda: (1,))

    class _DB:
        conn = _Conn()

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import tools.graph.db as graph_db
    monkeypatch.setattr(graph_db, "GraphDB", _DB)

    context = osd.prepare("org")

    assert context["delegate_metadata"]["key_exists"] is False
    assert osd.signing_key("org") is None   # what 'reuse' would hit
