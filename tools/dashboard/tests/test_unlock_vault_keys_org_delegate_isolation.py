"""Vault bring-up must not be un-done by one organization's delegate.

``POST /api/identity/unlock/vault-keys`` brings the PERSONAL vault up first,
then accepts each organization's storage delegate. These tests inject a
delegate refusal after successful personal vault warm-up; they do not infer
the cause of any live refusal. Previously the shared exception handler
returned 500 and skipped the snapshot, pepper and certificate reconcile.

Expected: the vault outcome and each organization's delegate outcome are
reported separately; a per-organization refusal never masks a warm vault
or skips the steps that make it durable.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import org_storage_delegate, unlock_routes


@pytest.fixture
def warm(monkeypatch):
    """A vault that brings up cleanly; records the post-success steps."""
    calls: dict[str, int] = {"snapshot": 0, "pepper": 0, "reconcile": 0, "bring_up": 0}
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda request: {"sid": "t"})
    monkeypatch.setattr(unlock_routes, "_personal_store_has_generations", lambda: False)

    def bring_up(decoded):
        calls["bring_up"] += 1
        return 0

    monkeypatch.setattr(unlock_routes, "_bring_vault_up", bring_up)

    def snapshot():
        calls["snapshot"] += 1
        return True

    monkeypatch.setattr(unlock_routes, "save_vault_across_hot_reload", snapshot)

    def pepper():
        calls["pepper"] += 1

    monkeypatch.setattr(unlock_routes, "_ensure_sealed_settings_pepper", pepper)
    monkeypatch.setattr(unlock_routes, "_validate_audited_delegate_pair",
                        lambda private, public: (private, public))
    monkeypatch.setattr(unlock_routes, "_assert_audited_recipient_compatible",
                        lambda public: None)
    monkeypatch.setattr(unlock_routes, "_install_personal_audited_delegate",
                        lambda private, public: None)
    from tools.dashboard import service_certificate_manager

    def reconcile():
        calls["reconcile"] += 1

    monkeypatch.setattr(service_certificate_manager, "request_reconcile", reconcile)
    return calls


def _client():
    return TestClient(Starlette(routes=[
        Route("/api/identity/unlock/vault-keys", unlock_routes.post_unlock_vault_keys,
              methods=["POST"]),
    ]))


def _delegates_where_one_is_stale(monkeypatch):
    accepted: list[str] = []

    def accept(item):
        if item["organization"] == "stale":
            raise ValueError("organization delegate must cite current heads")
        accepted.append(item["organization"])

    monkeypatch.setattr(org_storage_delegate, "accept", accept)
    return accepted, [
        {"organization": "stale", "action": "new", "private_key": "00" * 32,
         "event": "{}"},
        {"organization": "fresh", "action": "reuse", "key_reference": "k1"},
    ]


def test_stale_delegate_does_not_report_a_warm_vault_as_failed(warm, monkeypatch):
    accepted, delegates = _delegates_where_one_is_stale(monkeypatch)

    with _client() as client:
        response = client.post("/api/identity/unlock/vault-keys", json={
            "generation_keys": {}, "organization_delegates": delegates,
        })

    assert warm["bring_up"] == 1
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True                      # the vault IS up
    assert accepted == ["fresh"]                   # the other org still landed
    # The refused organization is named, with its reason, not folded into a
    # vault error.
    outcomes = body.get("organization_delegates") or {}
    assert "stale" in outcomes and "current heads" in str(outcomes["stale"])
    # And the warm process was made durable exactly as on a clean bring-up.
    assert warm["snapshot"] == 1
    assert body["snapshot_persisted"] is True


def test_stale_delegate_does_not_skip_the_recipient_post_steps(warm, monkeypatch):
    """With an audited recipient in the same request, the pepper seal and the
    certificate reconcile are the state transitions that unblock issuance;
    an unrelated organization's refusal must not skip them."""
    _accepted, delegates = _delegates_where_one_is_stale(monkeypatch)

    with _client() as client:
        response = client.post("/api/identity/unlock/vault-keys", json={
            "generation_keys": {},
            "delegate_audited_private_key": "11" * 32,
            "delegate_audited_public_key": "22" * 32,
            "organization_delegates": delegates,
        })

    assert response.status_code == 200, response.text
    assert warm["reconcile"] == 1
    assert warm["pepper"] == 1
    assert warm["snapshot"] == 1


def test_vault_bring_up_failure_is_still_a_refusal(warm, monkeypatch):
    """The isolation must not soften a REAL vault failure: when the vault
    itself cannot come up, nothing is accepted and the caller hears it."""
    def broken(decoded):
        raise RuntimeError("generation key does not open state s1")

    monkeypatch.setattr(unlock_routes, "_bring_vault_up", broken)
    accepted, delegates = _delegates_where_one_is_stale(monkeypatch)

    with _client() as client:
        response = client.post("/api/identity/unlock/vault-keys", json={
            "generation_keys": {}, "organization_delegates": delegates,
        })

    assert response.status_code == 500
    assert "could not be brought up" in response.json()["error"]
    assert accepted == []
    assert warm["snapshot"] == 0
