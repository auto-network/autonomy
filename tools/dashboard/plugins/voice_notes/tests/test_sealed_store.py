"""The note two-compartment storage over SealedSettings + a fake settings_ops."""
from types import SimpleNamespace

import pytest

from tools.graph.sealed_settings import OpsBackend, SealedSettings
from tools.dashboard.plugins.voice_notes.entrypoints import sealed_store as ss


class FakeOps:
    """Fake settings_ops: audited + sealed-row sets, server derives <org>: prefix."""
    AUDITED = "autonomy.vault.audited"
    ROWS = "autonomy.sealed-settings.row"

    def __init__(self):
        self.store = {self.AUDITED: {}, self.ROWS: {}}

    def read_set(self, set_id, *, org, peers=None):
        val_field = "value" if set_id == self.AUDITED else "ciphertext"
        members = [SimpleNamespace(key=k, payload={val_field: v}, vault_error=None)
                   for k, v in self.store.get(set_id, {}).items()]
        return SimpleNamespace(members=members)

    def read_set_key(self, set_id, key, *, org, peers=None):
        # scoping: an org caller reads its own <org>: rows
        want = f"{org}:{key}" if org else key
        v = self.store.get(set_id, {}).get(want)
        return {"payload": {"value": v}} if v is not None else None

    def write_by_key(self, set_id, rev, key, payload, *, org, state):
        derived = f"{org}:{key}" if org else key
        self.store.setdefault(set_id, {})[derived] = (
            payload.get("value") if "value" in payload else payload.get("ciphertext")
        )


@pytest.fixture
def store_and_ops(monkeypatch):
    fake = FakeOps()
    monkeypatch.setattr(OpsBackend, "_ops", lambda self: fake)
    store = SealedSettings("voice-notes", OpsBackend("autonomy", sealed_index=bytes(range(32))))
    return store, fake


def test_create_read_roundtrip(store_and_ops):
    store, fake = store_and_ops
    token = ss.create_note(store, fake, "autonomy", note_id="n1",
                           title="Groceries", body="milk, eggs",
                           bound_session="auto-0820-141006")
    assert isinstance(token, str) and len(token) > 20
    view = ss.read_note(store, fake, "autonomy", note_id="n1")
    assert view.title == "Groceries" and view.body == "milk, eggs"
    # The body lives in the AUDITED set, not the metadata row.
    assert any(k.startswith("autonomy:") for k in fake.store[FakeOps.AUDITED]
               if not k.endswith("sealed-settings.pepper"))


def test_token_hash_stored_not_raw(store_and_ops):
    store, fake = store_and_ops
    token = ss.create_note(store, fake, "autonomy", note_id="n1",
                           title="t", body="b", bound_session="s")
    th, bs = ss.access_fields(store, "n1")
    assert ss.token_matches(token, th) and not ss.token_matches("wrong", th)
    assert bs == "s"
    # The raw token appears in NO stored value (sealed-row or audited).
    import json
    dump = json.dumps({**fake.store[FakeOps.ROWS], **fake.store[FakeOps.AUDITED]})
    assert token not in dump


def test_list_shows_titles_not_bodies(store_and_ops):
    store, fake = store_and_ops
    ss.create_note(store, fake, "autonomy", note_id="a", title="A", body="secretA", bound_session="s")
    ss.create_note(store, fake, "autonomy", note_id="b", title="B", body="secretB", bound_session="s")
    titles = ss.list_titles(store)
    assert {t["title"] for t in titles} == {"A", "B"}
    assert all("body" not in t for t in titles)


def test_revoke_clears_token(store_and_ops):
    store, fake = store_and_ops
    token = ss.create_note(store, fake, "autonomy", note_id="n1", title="t", body="b", bound_session="s")
    assert ss.revoke_note(store, "n1") is True
    th, _ = ss.access_fields(store, "n1")
    assert th == "" and not ss.token_matches(token, th)
