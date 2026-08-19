"""The vault comes up at unlock, or it does not come up at all.

The generation keys are opened in the BROWSER — the personal root never
reaches this process — so bringing the vault up means receiving content keys
over a route and installing the read and write seams with them. That route is
the difference between a vault that is built and a vault that works.

Every refusal here is about not half-installing: a wrong-length secret, an
empty set, or an unauthenticated caller each leave the process with no vault
rather than a vault that fails later at a read, where it would look like a
key-agreement problem instead of a wiring one.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import unlock_routes
from tools.graph import settings_ops


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(unlock_routes, "_VAULT_CACHE", {})
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    app = Starlette(routes=[
        Route("/api/identity/unlock/vault-keys",
              unlock_routes.post_unlock_vault_keys, methods=["POST"]),
    ])
    with TestClient(app) as c:
        yield c
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(unlock_routes, "session_from_request",
                        lambda request: {"sid": "s-1", "method": "password"})


def _keys(n=1):
    return {f"state-{i}": ("%02x" % i) * 32 for i in range(n)}


# ── the gate ─────────────────────────────────────────────────


def test_without_a_session_the_vault_does_not_come_up(client, monkeypatch):
    """THE ONE THAT MATTERS. This runs AFTER proving possession of the root;
    a caller with no session has proved nothing, and the keys it offers could
    be anything."""
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda r: None)

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": _keys()})

    assert response.status_code == 401
    assert settings_ops._vault_key_holder is None, "a read seam was installed"
    assert settings_ops._vault_sealer is None, "a write seam was installed"


# ── what it refuses rather than half-installing ──────────────


def test_an_empty_set_is_refused(client, unlocked):
    """The browser opening no grants is a failure to report, not a vault to
    bring up half-way."""
    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {}})

    assert response.status_code == 400
    assert settings_ops._vault_key_holder is None


def test_a_wrong_length_secret_is_refused(client, unlocked):
    """Caching a 16-byte 'generation key' succeeds here and fails at a READ,
    where it looks like key agreement rather than a bad hand-off."""
    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {"state-1": "ab" * 16}})

    assert response.status_code == 400
    assert "32" in response.json()["error"]
    assert settings_ops._vault_key_holder is None


def test_non_hex_is_refused(client, unlocked):
    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {"state-1": "zz" * 32}})

    assert response.status_code == 400
    assert settings_ops._vault_key_holder is None


# ── what it installs ─────────────────────────────────────────


def test_a_good_hand_off_installs_both_seams(client, unlocked):
    """Before: neither a read nor a write is possible in this process."""
    assert settings_ops._vault_key_holder is None
    assert settings_ops._vault_sealer is None

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": _keys(2)})

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "generations": 2}
    assert settings_ops._vault_key_holder is not None, "no read path"
    assert settings_ops._vault_sealer is not None, "no write path"


def test_a_second_organization_adds_to_the_same_cache(client, unlocked):
    """Unlocking another org must not drop the first one's keys — that would
    surface as an audited read failing for one org after unlocking another."""
    client.post("/api/identity/unlock/vault-keys",
                json={"generation_keys": {"state-a": "aa" * 32}})

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {"state-b": "bb" * 32}})

    assert response.json()["generations"] == 2


def test_a_write_still_fails_closed_without_a_delegate(client, unlocked):
    """The vault being UP is not the vault being usable for writes. Until the
    delegate ceremony is wired there is no author, and a write must refuse
    naming the unlock rather than authoring as something it should not."""
    from tools.vault.key_sealer import VaultSealerNotReady
    import tools.graph.schemas  # noqa: F401

    client.post("/api/identity/unlock/vault-keys",
                json={"generation_keys": _keys()})

    with pytest.raises(VaultSealerNotReady, match="delegate"):
        settings_ops.add_setting(
            "autonomy.vault.audited", 1, "github.token",
            {"value": "x" * 40}, org=None,
        )
