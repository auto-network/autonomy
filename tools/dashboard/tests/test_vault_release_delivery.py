"""The vault-open plaintext lands only in the session's PRIVATE ramfs.

The delivered artifact is the VALUE in its final shape — raw credential
bytes at ``/run/secrets/<credential-name>`` inside the requesting
container's own mount namespace — never an envelope, never a shared host
path. The ledger commits before the helper runs.
"""

from __future__ import annotations

import json

import pytest

from tools.dashboard import vault_release_delivery as delivery
from tools.dashboard.dao import vault_releases


@pytest.fixture(autouse=True)
def _machine_store(tmp_path, monkeypatch):
    """Release leases are machine-homed Settings; isolate via the data root."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def helper(monkeypatch):
    """Capture the in-container write; the real one needs a live container."""
    calls: list[tuple] = []

    def fake_deliver(container, filename, data, **_kw):
        calls.append((container, filename, data, vault_releases.get("release-1")))
        return f"/run/secrets/{filename}"

    monkeypatch.setattr(delivery, "deliver_secret_file", fake_deliver)
    return calls


def _row(expires_at=200.0, key="mac.ssh"):
    return {
        "id": "release-1",
        "session": "auto-requester",
        "request": {
            "target": f"autonomy.vault.secured/{key}",
            "expires_at": expires_at,
            "setting": {
                "set_id": "autonomy.vault.secured",
                "key": key,
            },
        },
    }


def test_record_first_then_private_write_returns_only_receipt(helper):
    secret = "private-material-must-not-enter-receipt"
    receipt = delivery.deliver_payload(_row(), {"value": secret}, now=100.0)

    assert receipt == {
        "release_id": "release-1",
        "delivery": "session-ramfs",
        "path": "/run/secrets/mac.ssh",
        "lifetime": "session",
    }
    (container, filename, data, record_at_write) = helper[0]
    assert container == "auto-requester"
    assert filename == "mac.ssh"
    # RAW bytes, byte-identical to the sealed value — no envelope.
    assert data == secret.encode()
    # The durable record committed BEFORE the helper ran.
    assert record_at_write is not None
    record = vault_releases.get("release-1")
    assert record["container_path"] == "/run/secrets/mac.ssh"
    # No host path exists — the locator names the container namespace.
    assert record["host_path"].startswith("container-ns:auto-requester:")
    # Session lifetime: no deadline; the mount dies with the container.
    assert record["expires_at"] is None
    assert secret not in json.dumps(record)
    assert secret not in json.dumps(receipt)


def test_org_derived_key_delivers_under_its_bare_credential_name(helper):
    receipt = delivery.deliver_payload(
        _row(key="blindhash:fleet-ssh-key"), {"value": "KEY BYTES"}, now=100.0,
    )
    assert receipt["path"] == "/run/secrets/fleet-ssh-key"
    assert helper[0][1] == "fleet-ssh-key"


def test_structured_payload_without_single_value_is_refused(helper):
    """Envelopes are for the store, never the consumer: a structured secret
    delivers its document AS the value."""
    with pytest.raises(delivery.VaultDeliveryError, match="final shape"):
        delivery.deliver_payload(
            _row(), {"private_key": "no", "port": 22}, now=100.0,
        )
    assert helper == []
    assert vault_releases.get("release-1") is None


def test_expired_release_never_reaches_the_helper(helper):
    with pytest.raises(delivery.VaultDeliveryError, match="expired"):
        delivery.deliver_payload(_row(expires_at=50.0), {"value": "v"}, now=100.0)
    assert helper == []
    assert vault_releases.get("release-1") is None


def test_helper_failure_closes_the_ledger_as_delivery_failed(monkeypatch):
    from agents.secret_ramfs import ProvisionError

    def refuse(container, filename, data, **_kw):
        raise ProvisionError("container not running")

    monkeypatch.setattr(delivery, "deliver_secret_file", refuse)
    with pytest.raises(delivery.VaultDeliveryError, match="not running"):
        delivery.deliver_payload(_row(), {"value": "v"}, now=100.0)
    record = vault_releases.get("release-1")
    assert record["shred_reason"] == "delivery_failed"


def test_unsafe_session_or_release_names_are_refused(helper):
    bad = _row()
    bad["session"] = "../escape"
    with pytest.raises(delivery.VaultDeliveryError, match="unsafe session"):
        delivery.deliver_payload(bad, {"value": "v"}, now=100.0)
    assert helper == []
