"""The vault-open plaintext destination is non-swappable session ramfs.

The delivered artifact is the VALUE in its final shape — raw credential
bytes at ``/run/secrets/<credential-name>``, session lifetime — never an
envelope a consumer would have to unwrap.
"""

from __future__ import annotations

import json
import stat

import pytest

from tools.dashboard import vault_release_delivery as delivery
from tools.dashboard.dao import vault_releases
from tools.network.storagekit.memory_cache import MemoryClassError


@pytest.fixture(autouse=True)
def _machine_store(tmp_path, monkeypatch):
    """Release leases are machine-homed Settings; isolate via the data root."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)


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


def test_record_then_ramfs_write_returns_only_receipt(tmp_path, monkeypatch):
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    session_dir.chmod(0o700)
    other_session_dir = root / "auto-other"
    other_session_dir.mkdir()
    other_session_dir.chmod(0o700)
    checked = []
    monkeypatch.setattr(
        delivery,
        "assert_memory_backed",
        lambda path: checked.append(path),
    )

    secret = "private-material-must-not-enter-receipt"
    receipt = delivery.deliver_payload(
        _row(),
        {"value": secret},
        delivery_root=root,
        now=100.0,
    )

    assert checked == [session_dir]
    assert receipt == {
        "release_id": "release-1",
        "delivery": "session-ramfs",
        "path": "/run/secrets/mac.ssh",
        "lifetime": "session",
    }
    release_file = session_dir / "mac.ssh"
    # RAW bytes, byte-identical to the sealed value — no envelope, nothing
    # for the consumer to unwrap.
    assert release_file.read_text() == secret
    assert stat.S_IMODE(release_file.stat().st_mode) == 0o600
    assert list(other_session_dir.iterdir()) == []
    record = vault_releases.get("release-1")
    assert record["container_path"] == receipt["path"]
    # Session lifetime: no deadline — destroyed at session end/orphan sweep.
    assert record["expires_at"] is None
    assert secret not in json.dumps(record)
    assert secret not in json.dumps(receipt)


def test_org_derived_key_delivers_under_its_bare_credential_name(
    tmp_path, monkeypatch,
):
    """A routed key like ``blindhash:fleet-ssh-key`` lands at the stable
    consumer path ``/run/secrets/fleet-ssh-key`` — the suffix the session
    asked for, without the server-derived org prefix."""
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    session_dir.chmod(0o700)
    monkeypatch.setattr(delivery, "assert_memory_backed", lambda _p: None)

    receipt = delivery.deliver_payload(
        _row(key="blindhash:fleet-ssh-key"),
        {"value": "OPENSSH KEY BYTES"},
        delivery_root=root,
        now=100.0,
    )
    assert receipt["path"] == "/run/secrets/fleet-ssh-key"
    assert (session_dir / "fleet-ssh-key").read_text() == "OPENSSH KEY BYTES"


def test_structured_payload_without_single_value_is_refused(
    tmp_path, monkeypatch,
):
    """Envelopes are for the store, never the consumer: a payload that is
    not a single value cannot be delivered — a structured secret delivers
    its document AS the value."""
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    session_dir.chmod(0o700)
    monkeypatch.setattr(delivery, "assert_memory_backed", lambda _p: None)

    with pytest.raises(delivery.VaultDeliveryError, match="final shape"):
        delivery.deliver_payload(
            _row(),
            {"private_key": "no", "port": 22},
            delivery_root=root,
            now=100.0,
        )
    assert vault_releases.get("release-1") is None
    assert list(session_dir.iterdir()) == []


def test_re_release_replaces_the_same_credential_file(tmp_path, monkeypatch):
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    session_dir.chmod(0o700)
    monkeypatch.setattr(delivery, "assert_memory_backed", lambda _p: None)

    delivery.deliver_payload(
        _row(), {"value": "first"}, delivery_root=root, now=100.0,
    )
    second = _row()
    second["id"] = "release-2"
    delivery.deliver_payload(
        second, {"value": "second"}, delivery_root=root, now=101.0,
    )
    assert (session_dir / "mac.ssh").read_text() == "second"
    assert stat.S_IMODE((session_dir / "mac.ssh").stat().st_mode) == 0o600
    # Both leases exist; destruction tolerates the shared, replaced path.
    assert vault_releases.get("release-1") is not None
    assert vault_releases.get("release-2") is not None


def test_non_ramfs_destination_fails_before_ledger_or_plaintext(tmp_path, monkeypatch):
    root = tmp_path / "not-ramfs"
    (root / "auto-requester").mkdir(parents=True)
    (root / "auto-requester").chmod(0o700)

    def refuse(_path):
        raise MemoryClassError("ordinary disk is not ramfs")

    monkeypatch.setattr(delivery, "assert_memory_backed", refuse)
    with pytest.raises(MemoryClassError, match="not ramfs"):
        delivery.deliver_payload(
            _row(),
            {"value": "never-encode-me"},
            delivery_root=root,
            now=100.0,
        )

    assert vault_releases.get("release-1") is None
    assert list((root / "auto-requester").iterdir()) == []


def test_session_directory_mode_and_owner_are_mandatory(tmp_path, monkeypatch):
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(delivery, "assert_memory_backed", lambda _path: None)

    session_dir.chmod(0o755)
    with pytest.raises(delivery.VaultDeliveryError, match="mode 0700"):
        delivery.deliver_payload(
            _row(), {"value": "no"}, delivery_root=root,
            now=100.0,
        )
    session_dir.chmod(0o700)
    monkeypatch.setattr(delivery, "SESSION_SECRET_UID", session_dir.stat().st_uid + 1)
    with pytest.raises(delivery.VaultDeliveryError, match="wrong owner"):
        delivery.deliver_payload(
            _row(), {"value": "still-no"}, delivery_root=root,
            now=100.0,
        )

    assert vault_releases.get("release-1") is None
    assert list(session_dir.iterdir()) == []


def test_failed_materialisation_is_unlinked_and_closed_in_ledger(
    tmp_path, monkeypatch,
):
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    session_dir.chmod(0o700)
    monkeypatch.setattr(delivery, "assert_memory_backed", lambda _path: None)
    monkeypatch.setattr(
        delivery,
        "_write_all",
        lambda *_args: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )

    with pytest.raises(OSError, match="synthetic"):
        delivery.deliver_payload(
            _row(),
            {"value": "partial-must-disappear"},
            delivery_root=root,
            now=100.0,
        )

    assert not (session_dir / "mac.ssh").exists()
    record = vault_releases.get("release-1")
    assert record["shred_reason"] == "delivery_failed"
    assert record["shredded_at"] == 100_000
