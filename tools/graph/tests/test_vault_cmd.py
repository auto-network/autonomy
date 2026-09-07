"""The `graph vault` verb layer dispatches correctly across the two tiers."""

from __future__ import annotations

import argparse

import pytest

from tools.graph import vault_cmd


class FakeMember:
    def __init__(self, key, payload=None, vault_error=None, sealed=None):
        self.key = key
        self.id = "id-" + key
        self.payload = payload
        self.vault_error = vault_error
        self.sealed_content_key = sealed


class FakeMembers:
    def __init__(self, members):
        self.members = members


class FakeClient:
    def __init__(self, sets=None):
        self.sets = sets or {}
        self.sealed = []
        self.added = []
        self.removed = []

    def read_set(self, set_id, *, org=None, peers=None):
        return FakeMembers(self.sets.get(set_id, []))

    def seal_personal_setting(self, key, value, *, policy_class_id):
        self.sealed.append((key, value, policy_class_id))
        return "sid-secured-0"

    def add_setting(self, set_id, rev, key, payload, *, org=None, **kw):
        self.added.append((set_id, key, payload, org))
        return "sid-audited-0"

    def remove_vault_credential(self, set_id, name, *, org):
        self.removed.append((set_id, name, org))


def _args(**kw):
    d = dict(
        name="k", org=None, tier="secured", audience=None, retier=False,
        secret_file=None, secret_fd=None, secret_prompt=False, wait=0,
    )
    d.update(kw)
    return argparse.Namespace(**d)


def _use(monkeypatch, client, secret=b"S"):
    monkeypatch.setattr(vault_cmd, "get_client", lambda: client)
    monkeypatch.setattr(vault_cmd, "_read_secret_bytes", lambda a: bytearray(secret))


def test_seal_secured_uses_the_personal_seal_seam(monkeypatch):
    c = FakeClient()
    _use(monkeypatch, c, b"ghp_x")
    vault_cmd.cmd_vault_seal(_args(name="gh.token", tier="secured"))
    assert c.sealed == [("gh.token", "ghp_x", "personal-root")]
    assert not c.added


def test_seal_audited_uses_add_setting_to_the_audited_set(monkeypatch):
    c = FakeClient()
    _use(monkeypatch, c, b"ghp_x")
    vault_cmd.cmd_vault_seal(_args(name="gh.token", tier="audited"))
    assert c.added and c.added[0][0] == vault_cmd.VAULT_AUDITED_SET_ID
    assert c.added[0][2] == {"value": "ghp_x"}
    assert not c.sealed


def test_to_on_audited_is_an_error(monkeypatch):
    c = FakeClient()
    _use(monkeypatch, c)
    with pytest.raises(SystemExit):
        vault_cmd.cmd_vault_seal(_args(name="k", tier="audited", audience="cls"))
    assert not c.added and not c.sealed


def test_an_explicit_org_prefix_in_the_name_is_an_error(monkeypatch):
    c = FakeClient()
    _use(monkeypatch, c)
    with pytest.raises(SystemExit):
        vault_cmd.cmd_vault_seal(_args(name="anchore:k"))


def test_tier_is_immutable_per_name_without_retier(monkeypatch):
    c = FakeClient({vault_cmd.VAULT_AUDITED_SET_ID: [FakeMember("k")]})
    _use(monkeypatch, c)
    with pytest.raises(SystemExit):
        vault_cmd.cmd_vault_seal(_args(name="k", tier="secured"))
    assert not c.sealed and not c.removed


def test_retier_removes_from_the_other_tier_then_seals(monkeypatch):
    c = FakeClient({vault_cmd.VAULT_AUDITED_SET_ID: [FakeMember("k")]})
    _use(monkeypatch, c, b"S")
    vault_cmd.cmd_vault_seal(_args(name="k", tier="secured", retier=True))
    assert c.removed == [(vault_cmd.VAULT_AUDITED_SET_ID, "k", "personal")]
    assert c.sealed and c.sealed[0][0] == "k"


def test_remove_locates_the_tier_and_removes(monkeypatch):
    c = FakeClient({vault_cmd.VAULT_AUDITED_SET_ID: [FakeMember("k")]})
    _use(monkeypatch, c)
    vault_cmd.cmd_vault_remove(_args(name="k"))
    assert c.removed == [(vault_cmd.VAULT_AUDITED_SET_ID, "k", "personal")]


def test_secured_read_returns_a_pending_receipt_without_hanging(monkeypatch, capsys):
    c = FakeClient({vault_cmd.VAULT_SECURED_SET_ID: [FakeMember("gh.token", sealed={"x": 1})]})
    seen = {}

    def request_vault_open(set_id, name, *, org, wait_seconds):
        seen.update(set_id=set_id, name=name, org=org, wait_seconds=wait_seconds)
        return {"pending": True, "approval_id": "open-7", "name": name,
                "path": f"/run/secrets/{name}"}

    c.request_vault_open = request_vault_open
    monkeypatch.setattr(vault_cmd, "get_client", lambda: c)
    vault_cmd.cmd_vault_read(_args(name="gh.token", tier="secured", wait=0))
    out = capsys.readouterr().out
    # Default read is non-blocking: wait_seconds 0 → pending, and it announces
    # the approval id + the eventual path, never the value.
    assert seen["wait_seconds"] == 0
    assert seen["set_id"] == vault_cmd.VAULT_SECURED_SET_ID
    assert "pending" in out.lower()
    assert "open-7" in out
    assert "/run/secrets/gh.token" in out


def test_list_shows_names_and_tiers_never_values(monkeypatch, capsys):
    c = FakeClient({
        vault_cmd.VAULT_SECURED_SET_ID: [FakeMember("a")],
        vault_cmd.VAULT_AUDITED_SET_ID: [FakeMember("b", payload={"value": "SECRET"})],
    })
    _use(monkeypatch, c)
    vault_cmd.cmd_vault_list(_args())
    out = capsys.readouterr().out
    assert "a" in out and "b" in out
    assert "secured" in out and "audited" in out
    assert "SECRET" not in out


# ---- 2026-09-07: half-wired verbs found by the BlindHash hand-off ----------

def test_find_member_matches_the_bearer_prefixed_key():
    """`graph vault list` shows `<org>:name`; read/remove/seal must address it
    by the bare name (the server already scoped the listing)."""
    c = FakeClient({vault_cmd.VAULT_AUDITED_SET_ID: [FakeMember("blindhash:dnsmadeeasy.api-key")]})
    tier, member = vault_cmd._find_member(c, "dnsmadeeasy.api-key", "personal")
    assert tier == "audited" and member.key == "blindhash:dnsmadeeasy.api-key"
    assert vault_cmd._find_member(c, "api-key", "personal") == (None, None)  # no suffix matching


def test_audited_read_over_http_delivers_a_path_never_the_value(monkeypatch, capsys):
    member = FakeMember("autonomy:openrouter.api-key")
    member.payload = {"value": "sk-secret"}
    c = FakeClient({vault_cmd.VAULT_AUDITED_SET_ID: [member]})
    calls = []
    c.deliver_vault_credential = lambda set_id, name, *, org, ttl_seconds=0: (
        calls.append((set_id, name, org)) or {"delivery": "session-ramfs", "path": f"/run/secrets/{name}"}
    )
    _use(monkeypatch, c)
    vault_cmd.cmd_vault_read(_args(name="openrouter.api-key", org=vault_cmd._ORG_SENTINEL))
    out = capsys.readouterr()
    assert out.out.strip() == "/run/secrets/openrouter.api-key"
    assert "sk-secret" not in out.out + out.err
    assert calls == [(vault_cmd.VAULT_AUDITED_SET_ID, "openrouter.api-key", vault_cmd.CALLER_ORG)]


def test_audited_read_in_process_prints_inline_only_when_no_client_delivery_exists(monkeypatch, capsys):
    member = FakeMember("openrouter.api-key")
    member.payload = {"value": "sk-secret"}
    c = FakeClient({vault_cmd.VAULT_AUDITED_SET_ID: [member]})  # no deliver/open: direct-host mode
    _use(monkeypatch, c)
    vault_cmd.cmd_vault_read(_args(name="openrouter.api-key"))
    assert capsys.readouterr().out.strip() == "sk-secret"


def test_share_sends_no_org_header_by_default_and_the_slug_when_named(monkeypatch, capsys):
    calls = []
    c = FakeClient()
    c.share_vault_credential = lambda set_id, name, *, to_org, org, replace=False: (
        calls.append((set_id, name, to_org, org, replace)) or
        {"from_key": "blindhash:" + name, "to_key": to_org + ":" + name, "setting_id": "s1"}
    )
    _use(monkeypatch, c)
    vault_cmd.cmd_vault_share(_args(name="dnsmadeeasy.api-key", to_org="autonomy", replace=False, org=None))
    vault_cmd.cmd_vault_share(_args(name="dnsmadeeasy.api-key", to_org="autonomy", replace=True, org="blindhash"))
    assert calls[0] == (vault_cmd.VAULT_AUDITED_SET_ID, "dnsmadeeasy.api-key", "autonomy", None, False)
    assert calls[1][3] == "blindhash" and calls[1][4] is True
    out = capsys.readouterr().out
    assert "blindhash:dnsmadeeasy.api-key → autonomy:dnsmadeeasy.api-key" in out
