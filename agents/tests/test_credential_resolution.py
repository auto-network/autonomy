"""B1: `credential:<key>` env-binding resolution through the vault.

Covers the launcher's `_resolve_credential` (the vault read) and the
`credential:` branch of `_resolve_env_source` (routing + fail-closed drop).
The value must NEVER appear in a log line, and a cold vault must be
distinguishable from a genuinely-absent key.
"""
from __future__ import annotations

import logging
import types

import pytest

from agents import session_launcher as sl


class _Member:
    def __init__(self, key, payload=None, vault_error=None):
        self.key = key
        self.payload = payload
        self.vault_error = vault_error


class _Members:
    def __init__(self, members):
        self.members = members


def _patch_vault(monkeypatch, *, holder, members):
    """Point the launcher's vault read at a fake set with *members*, and set
    whether a key holder is registered (holder=None => cold vault)."""
    import tools.graph.ops as ops
    import tools.graph.settings_ops as settings_ops
    monkeypatch.setattr(settings_ops, "_vault_key_holder", holder, raising=False)
    monkeypatch.setattr(ops, "read_set", lambda set_id, **kw: _Members(members))


# ── _resolve_credential ──────────────────────────────────────────────────────

def test_resolve_credential_returns_value_when_warm_and_present(monkeypatch):
    _patch_vault(monkeypatch, holder=object(),
                 members=[_Member("github.token", {"value": "ghp_secret"})])
    assert sl._resolve_credential("github.token") == "ghp_secret"


def test_resolve_credential_cold_vault_returns_none(monkeypatch):
    # holder=None => vault never brought up. NOT the same as "key absent".
    _patch_vault(monkeypatch, holder=None,
                 members=[_Member("github.token", {"value": "ghp_secret"})])
    assert sl._resolve_credential("github.token") is None


def test_resolve_credential_absent_key_returns_none(monkeypatch):
    _patch_vault(monkeypatch, holder=object(),
                 members=[_Member("other.key", {"value": "x"})])
    assert sl._resolve_credential("github.token") is None


def test_resolve_credential_row_with_vault_error_returns_none(monkeypatch):
    err = types.SimpleNamespace(reason="VAULT_NO_KEY_HOLDER", message="cold")
    _patch_vault(monkeypatch, holder=object(),
                 members=[_Member("github.token", None, vault_error=err)])
    assert sl._resolve_credential("github.token") is None


def test_resolve_credential_malformed_payload_returns_none(monkeypatch):
    _patch_vault(monkeypatch, holder=object(),
                 members=[_Member("github.token", {"nope": "x"})])
    assert sl._resolve_credential("github.token") is None


def test_resolve_credential_never_logs_the_value(monkeypatch, caplog):
    _patch_vault(monkeypatch, holder=object(),
                 members=[_Member("github.token", {"value": "ghp_TOPSECRET"})])
    with caplog.at_level(logging.DEBUG):
        assert sl._resolve_credential("github.token") == "ghp_TOPSECRET"
    assert "ghp_TOPSECRET" not in caplog.text


# ── _resolve_env_source credential branch ────────────────────────────────────

def test_env_source_credential_routes_and_returns_value(monkeypatch):
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: "ghp_from_vault")
    assert sl._resolve_env_source("GH_TOKEN", "credential:github.token") == "ghp_from_vault"


def test_env_source_credential_drops_when_unavailable(monkeypatch):
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: None)
    assert sl._resolve_env_source("GH_TOKEN", "credential:github.token") is None


def test_env_source_credential_empty_key_drops(monkeypatch):
    called = []
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: called.append(key) or "x")
    assert sl._resolve_env_source("GH_TOKEN", "credential:") is None
    assert called == []  # never even attempted a vault read for an empty key


def test_env_source_credential_value_never_logged(monkeypatch, caplog):
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: "ghp_LEAKME")
    with caplog.at_level(logging.DEBUG):
        sl._resolve_env_source("GH_TOKEN", "credential:github.token")
    assert "ghp_LEAKME" not in caplog.text


# ── _declared_credential_keys ────────────────────────────────────────────────

def _cap(env_bindings):
    return types.SimpleNamespace(env_bindings=env_bindings)


def test_declared_credential_keys_extracts_only_credential_sources():
    caps = [
        _cap({"GH_TOKEN": "credential:github.token",
              "PLAIN": "host:SOME_VAR",
              "PULL": "credential:github.release-pull-token"}),
        _cap({"OTHER": "file:/etc/x:VAR", "EMPTY": "credential:"}),
    ]
    assert sl._declared_credential_keys(caps) == {
        "github.token", "github.release-pull-token",
    }  # host:/file:/empty-credential all excluded


def test_declared_credential_keys_empty_when_none_declared():
    assert sl._declared_credential_keys([_cap({"X": "host:Y"})]) == set()


# ── _credential_keys_in_env (workspace env / extra_env) ──────────────────────

def test_credential_keys_in_env_extracts_only_credential_values():
    env = {
        "GH_TOKEN": "credential:github.token",
        "PLAIN": "literal-value",
        "LOOKS_LIKE_HOST": "host:8080",       # literal, NOT a credential
        "FILE_URI": "file:///etc/x",          # literal, NOT a credential
        "PULL": "credential:github.release-pull-token",
        "EMPTY": "credential:",
    }
    assert sl._credential_keys_in_env(env) == {
        "github.token", "github.release-pull-token",
    }


def test_credential_keys_in_env_empty_cases():
    assert sl._credential_keys_in_env(None) == set()
    assert sl._credential_keys_in_env({}) == set()
    assert sl._credential_keys_in_env({"X": "plain", "Y": "host:Z"}) == set()
