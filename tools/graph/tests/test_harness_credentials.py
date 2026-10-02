"""Harness accounts in the vault (graph://5f2f5a49-00d v16 §10.9)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tools.graph import harness_credentials as hv
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.vault import key_holder
from tools.vault.personal_object import derive_delegate_audited_recipient
from tools.vault.store import VaultStore


@pytest.fixture
def warm_vault(tmp_path, monkeypatch):
    """A personal store whose audited vault seals (delegate recipient
    published, as first run does) and opens (delegate key warm, as unlock does)."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    db = tmp_path / "personal.db"
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: db)
    GraphDB(db).close()
    GraphDB.close_all_pooled()
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(private_hex)
    yield db
    GraphDB.close_all_pooled()
    settings_ops.set_personal_delegate_audited_key(None)


def test_keys_are_compound_and_refuse_bad_ids():
    assert hv.account_key("claude", "org-A", "setup") == "claude.account.org-A.setup"
    with pytest.raises(ValueError):
        hv.account_key("claude", "a:b", "setup")
    with pytest.raises(ValueError):
        hv.account_key("claude", "org-A", "nope")
    with pytest.raises(ValueError):
        hv.account_key("gemini", "x", "auth")


def test_write_read_and_change_an_account(warm_vault):
    hv.write_account("claude", "org-A", {
        "setup": "sk-ant-oat01-A", "alias": "gmail", "email": "a@example.com",
        "access": "at-1", "refresh": "rt-1", "expires": "9000",
        "scopes": hv.scopes_text(["user:profile", "user:inference"]),
    })
    acct = hv.read_account("claude", "org-A")
    assert acct is not None and acct.launchable
    assert acct.get("setup") == "sk-ant-oat01-A"
    assert hv.scopes_list(acct.get("scopes")) == ["user:profile", "user:inference"]
    assert acct.expires_ms() == 9000
    # a change appends a revision; the newest resolves
    hv.write_account("claude", "org-A", {"access": "at-2", "error": None})
    acct = hv.read_account("claude", "org-A")
    assert acct.get("access") == "at-2"
    assert acct.get("refresh") == "rt-1"
    assert acct.get("error") is None and acct.parts["error"] == hv.NONE
    assert [a.id for a in hv.list_accounts("claude")] == ["org-A"]
    assert hv.list_accounts("codex") == []


def test_rows_hold_no_plaintext(warm_vault):
    hv.write_account("codex", "acct-9", {"id": "id-secret", "access": "at-secret", "refresh": "rt-secret"})
    conn = sqlite3.connect(str(warm_vault))
    try:
        blobs = " ".join(str(r[0]) for r in conn.execute("SELECT payload FROM settings"))
    finally:
        conn.close()
    assert "secret" not in blobs
    assert hv.read_account("codex", "acct-9").launchable


def test_cold_vault_reports_present_but_not_openable(warm_vault):
    hv.write_account("grok", "default", {"auth": '{"t": 1}'})
    settings_ops.set_personal_delegate_audited_key(None)
    accts = hv.list_accounts("grok")
    assert [a.id for a in accts] == ["default"]
    assert accts[0].openable is False
    assert accts[0].get("auth") is None
    # sealing still works cold
    hv.write_account("grok", "default", {"alias": "work"})


def test_setup_token_freshness_is_by_minted_at():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    fresh = hv.Account("claude", "a", {"setup": "k", "setup_minted_at": (now - timedelta(days=10)).isoformat()})
    stale = hv.Account("claude", "b", {"setup": "k", "setup_minted_at": (now - timedelta(days=400)).isoformat()})
    none = hv.Account("claude", "c", {"access": "x", "refresh": "y"})
    assert fresh.setup_token_fresh(now) and not stale.setup_token_fresh(now) and not none.setup_token_fresh(now)


def test_remove_account_removes_every_row(warm_vault):
    hv.write_account("claude", "org-B", {"setup": "k", "alias": "b"})
    assert hv.remove_account("claude", "org-B") == 2
    assert hv.read_account("claude", "org-B") is None
    assert hv.remove_account("claude", "org-B") == 0


# ── the one-time migration of the pre-vault rows ─────────────


def _insert_plaintext(db, set_id, key, payload, created_at="2026-09-01T00:00:00Z"):
    import json, sqlite3, uuid
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO settings (id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at) VALUES (?, ?, 1, ?, ?, 'raw', ?, ?)",
            (str(uuid.uuid4()), set_id, key, json.dumps(payload), created_at, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    GraphDB.close_all_pooled()


def test_migration_seals_pre_vault_rows_and_deprecates_them(warm_vault):
    _insert_plaintext(warm_vault, "dashboard.claude.credentials", "org-A", {
        "alias": "gmail", "organization_name": "Org A", "account_email": "a@example.com",
        "access_token": "at-A", "refresh_token": "rt-A", "expires_at_ms": 9000,
        "scopes": ["user:profile", "user:inference"], "last_refresh_at": "2026-09-10T00:00:00Z",
    })
    _insert_plaintext(warm_vault, "dashboard.claude.setup_tokens", "org-A",
                      {"raw_key": "sk-ant-oat01-A"}, created_at="2026-09-05T00:00:00Z")
    _insert_plaintext(warm_vault, "dashboard.codex.credentials", "acct-9", {
        "auth_mode": "chatgpt", "email": "dev@example.com", "id_token": "id-9",
        "access_token": "ct-9", "refresh_token": "cr-9", "expires_at_ms": 7000,
    })
    counts = hv.migrate_plaintext_accounts()
    assert counts == {"claude": 1, "setup_tokens": 1, "codex": 1, "deprecated": 3,
                      "scrubbed": 3, "failed": 0}
    claude = hv.read_account("claude", "org-A")
    assert claude.get("setup") == "sk-ant-oat01-A"
    assert claude.get("setup_minted_at") == "2026-09-05T00:00:00Z"
    assert claude.get("alias") == "gmail" and claude.get("access") == "at-A"
    assert claude.expires_ms() == 9000 and claude.setup_token_fresh()
    codex = hv.read_account("codex", "acct-9")
    assert codex.launchable and codex.get("email") == "dev@example.com"
    # a second run finds nothing left
    assert hv.migrate_plaintext_accounts() == {"claude": 0, "setup_tokens": 0, "codex": 0,
                                               "deprecated": 0, "scrubbed": 0, "failed": 0}
    from tools.graph import ops
    assert ops.read_set("dashboard.claude.credentials", org="personal", peers=[]).members == []


# ── erasing the migrated rows' secrets (auto-se3e2) ──────────


def _stored(db, set_id):
    import json, sqlite3
    conn = sqlite3.connect(str(db))
    try:
        return [(json.loads(p), d) for p, d in conn.execute(
            "SELECT payload, deprecated FROM settings WHERE set_id = ?", (set_id,))]
    finally:
        conn.close()


def _seed_all(db):
    _insert_plaintext(db, "dashboard.claude.credentials", "org-A", {
        "alias": "gmail", "organization_name": "Org A", "account_email": "a@example.com",
        "access_token": "sec.access.claude", "refresh_token": "sec.refresh.claude", "expires_at_ms": 9000,
    })
    _insert_plaintext(db, "dashboard.claude.setup_tokens", "org-A",
                      {"raw_key": "sec.setup.claude"})
    _insert_plaintext(db, "dashboard.codex.credentials", "acct-9", {
        "email": "dev@example.com", "id_token": "sec.id.codex",
        "access_token": "sec.access.codex", "refresh_token": "sec.refresh.codex", "expires_at_ms": 7000,
    })


def test_a_migrated_row_keeps_no_secret_and_its_diagnosis(warm_vault):
    _seed_all(warm_vault)
    hv.migrate_plaintext_accounts()
    for set_id in ("dashboard.claude.credentials", "dashboard.claude.setup_tokens",
                   "dashboard.codex.credentials"):
        [(payload, deprecated)] = _stored(warm_vault, set_id)
        assert deprecated == 1
        assert not any(payload.get(f) for f in hv.RETIRED_SECRET_FIELDS), payload
    [(claude, _)] = _stored(warm_vault, "dashboard.claude.credentials")
    assert claude == {"alias": "gmail", "organization_name": "Org A",
                      "account_email": "a@example.com", "expires_at_ms": 9000}
    # ... and every account still reads from the vault.
    assert hv.read_account("claude", "org-A").get("refresh") == "sec.refresh.claude"
    assert hv.read_account("claude", "org-A").get("setup") == "sec.setup.claude"
    assert hv.read_account("codex", "acct-9").get("refresh") == "sec.refresh.codex"
    # No plaintext left anywhere in the file, free space included. The
    # secrets contain '.', which no base64url ciphertext does, so a hit is
    # a real copy and never a coincidence.
    wal = warm_vault.with_name(warm_vault.name + "-wal")
    raw = warm_vault.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    for secret in (b"sec.refresh.claude", b"sec.access.claude", b"sec.setup.claude", b"sec.refresh.codex", b"sec.access.codex", b"sec.id.codex"):
        assert secret not in raw, secret


def test_a_row_whose_vault_write_fails_keeps_its_secrets(warm_vault, monkeypatch):
    _seed_all(warm_vault)
    real = hv.write_account

    def fail_codex(harness, account_id, parts):
        if harness == "codex":
            raise RuntimeError("vault sealer unavailable")
        return real(harness, account_id, parts)

    monkeypatch.setattr(hv, "write_account", fail_codex)
    counts = hv.migrate_plaintext_accounts()
    assert counts["failed"] == 1 and counts["claude"] == 1 and counts["setup_tokens"] == 1
    [(codex, deprecated)] = _stored(warm_vault, "dashboard.codex.credentials")
    assert deprecated == 0
    assert codex["refresh_token"] == "sec.refresh.codex" and codex["access_token"] == "sec.access.codex"
    # The others were migrated and erased regardless.
    [(claude, _)] = _stored(warm_vault, "dashboard.claude.credentials")
    assert "refresh_token" not in claude


def test_rows_migrated_before_the_erasure_are_scrubbed_once_confirmed(warm_vault):
    """A node migrated by the previous code holds deprecated rows with their
    secrets; the next startup erases them, but only once the vault copy is
    confirmed, and a second startup changes nothing."""
    _seed_all(warm_vault)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hv, "scrub_migrated_secrets", lambda: 0)
        hv.migrate_plaintext_accounts()                 # the old behaviour
    [(before, _)] = _stored(warm_vault, "dashboard.claude.credentials")
    assert before["refresh_token"] == "sec.refresh.claude"

    # Vault copy unconfirmed (cold): nothing is erased.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(hv, "read_account", lambda *a, **k: None)
        assert hv.migrate_plaintext_accounts()["scrubbed"] == 0
    [(still, _)] = _stored(warm_vault, "dashboard.claude.credentials")
    assert still["refresh_token"] == "sec.refresh.claude"

    assert hv.migrate_plaintext_accounts()["scrubbed"] == 3
    snapshot = {s: _stored(warm_vault, s) for s in (
        "dashboard.claude.credentials", "dashboard.claude.setup_tokens",
        "dashboard.codex.credentials")}
    assert hv.migrate_plaintext_accounts() == {"claude": 0, "setup_tokens": 0, "codex": 0,
                                               "deprecated": 0, "scrubbed": 0, "failed": 0}
    assert snapshot == {s: _stored(warm_vault, s) for s in snapshot}


def test_listing_one_harness_opens_only_that_harness_s_rows(warm_vault, monkeypatch):
    """auto-k784w review: the read filters by key prefix in the query, so the
    vault opens no other secret of the tier -- not another harness's
    accounts, not an unrelated credential -- to list one harness."""
    hv.write_account("claude", "org-A", {"alias": "a", "access": "at-1", "refresh": "rt-1"})
    hv.write_account("codex", "acct-9", {"id": "i", "access": "at-c", "refresh": "rt-c"})
    hv._write("relay.token", "unrelated-secret", None)
    opened = []
    real = settings_ops._unwrap_vault_locator

    def spy(locator, **kw):
        opened.append(kw["key"])
        return real(locator, **kw)

    monkeypatch.setattr(settings_ops, "_unwrap_vault_locator", spy)
    assert [a.id for a in hv.list_accounts("claude")] == ["org-A"]
    assert opened and all(k.startswith("claude.account.") for k in opened)


def test_key_prefix_is_literal_and_escaped(warm_vault):
    hv.write_account("claude", "org_A", {"alias": "underscore"})
    hv.write_account("claude", "orgxA", {"alias": "x"})
    rows = settings_ops.read_set(hv.VAULT_AUDITED_SET_ID, org=None, peers=[],
                                 key_prefix="claude.account.org_A.")
    assert {r.key for r in rows.members} == {"claude.account.org_A.alias"}
