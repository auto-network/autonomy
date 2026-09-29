"""The settings signer is resolved once per delegate lifetime, not per
write (auto-qrmlg.6 S3; S2 as merged opened and hydrated the ledger store
on every organization write, ~8 ms on SJC-2, 2026-09-29)."""

from __future__ import annotations

import time

import pytest

from tools.dashboard import org_storage_delegate as osd
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import NETWORK_STORAGE_DELEGATE_SET_ID
from tools.network.idkit import KeyPair

GENESIS = "ab" * 32
PERSONA = "cd" * 32


@pytest.fixture
def resolver(monkeypatch):
    key = KeyPair.generate()
    calls = {"key": 0, "fold": 0, "expires_at": int(time.time() * 1000) + 3_600_000}

    def signing_key(org):
        calls["key"] += 1
        return key

    def resolve(org, public_hex, now):
        calls["fold"] += 1
        return GENESIS, PERSONA

    def read_set_key(set_id, key_, org=None):
        assert set_id == NETWORK_STORAGE_DELEGATE_SET_ID and key_ == GENESIS
        return {"payload": {"expires_at": calls["expires_at"]}}

    monkeypatch.setattr(osd, "signing_key", signing_key)
    monkeypatch.setattr(osd, "_resolve_signer_persona", resolve)
    monkeypatch.setattr(settings_ops, "read_set_key", read_set_key)
    monkeypatch.setattr(osd, "_SIGNING_CONTEXTS", {})
    return key, calls


def test_the_context_is_resolved_once_and_reused_until_the_delegate_expires(resolver):
    key, calls = resolver
    first = osd.signing_context("anchore")
    assert first.key is key and first.terminal_persona == PERSONA and first.genesis_id == GENESIS
    for _ in range(50):
        assert osd.signing_context("anchore") is first
    assert calls == {"key": 1, "fold": 1, "expires_at": calls["expires_at"]}
    # Forgetting (a renewed or replaced delegate) resolves again.
    osd.forget_signing_context("anchore")
    assert osd.signing_context("anchore") is not first
    assert calls["key"] == 2 and calls["fold"] == 2


def test_an_expired_delegate_is_never_cached(resolver):
    key, calls = resolver
    calls["expires_at"] = int(time.time() * 1000) - 1
    a = osd.signing_context("anchore")
    b = osd.signing_context("anchore")
    assert a is not None and b is not None and a is not b
    assert calls["key"] == 2 and calls["fold"] == 2


def test_no_persona_means_no_signer(resolver, monkeypatch):
    monkeypatch.setattr(osd, "_resolve_signer_persona", lambda org, pub, now: (GENESIS, None))
    assert osd.signing_context("anchore") is None
    assert osd._SIGNING_CONTEXTS == {}
