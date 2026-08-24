"""The vault-open plaintext destination is non-swappable session ramfs."""

from __future__ import annotations

import json
import stat

import pytest

from tools.dashboard import vault_release_delivery as delivery
from tools.dashboard.dao import vault_releases
from tools.network.storagekit.memory_cache import MemoryClassError


def _row(expires_at=200.0):
    return {
        "id": "release-1",
        "session": "auto-requester",
        "request": {
            "target": "autonomy.vault.secured/mac.ssh",
            "expires_at": expires_at,
            "setting": {
                "set_id": "autonomy.vault.secured",
                "key": "mac.ssh",
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
    ledger = tmp_path / "releases.db"
    checked = []
    monkeypatch.setattr(
        delivery,
        "assert_memory_backed",
        lambda path: checked.append(path),
    )

    secret = "private-material-must-not-enter-receipt"
    receipt = delivery.deliver_payload(
        _row(),
        {"private_key": secret, "port": 22},
        delivery_root=root,
        store_path=ledger,
        now=100.0,
    )

    assert checked == [session_dir]
    assert receipt == {
        "release_id": "release-1",
        "delivery": "session-ramfs",
        "path": "/run/secrets/vault-open-release-1.json",
        "expires_at": 200.0,
    }
    release_file = session_dir / "vault-open-release-1.json"
    assert json.loads(release_file.read_text()) == {
        "private_key": secret,
        "port": 22,
    }
    assert stat.S_IMODE(release_file.stat().st_mode) == 0o600
    assert list(other_session_dir.iterdir()) == []
    record = vault_releases.get("release-1", path=ledger)
    assert record["container_path"] == receipt["path"]
    assert record["expires_at"] == 200_000
    assert secret not in json.dumps(record)
    assert secret not in json.dumps(receipt)


def test_non_ramfs_destination_fails_before_ledger_or_plaintext(tmp_path, monkeypatch):
    root = tmp_path / "not-ramfs"
    (root / "auto-requester").mkdir(parents=True)
    (root / "auto-requester").chmod(0o700)
    ledger = tmp_path / "releases.db"

    def refuse(_path):
        raise MemoryClassError("ordinary disk is not ramfs")

    monkeypatch.setattr(delivery, "assert_memory_backed", refuse)
    with pytest.raises(MemoryClassError, match="not ramfs"):
        delivery.deliver_payload(
            _row(),
            {"private_key": "never-encode-me"},
            delivery_root=root,
            store_path=ledger,
            now=100.0,
        )

    assert vault_releases.get("release-1", path=ledger) is None
    assert list((root / "auto-requester").iterdir()) == []


def test_session_directory_mode_and_owner_are_mandatory(tmp_path, monkeypatch):
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    ledger = tmp_path / "releases.db"
    monkeypatch.setattr(delivery, "assert_memory_backed", lambda _path: None)

    session_dir.chmod(0o755)
    with pytest.raises(delivery.VaultDeliveryError, match="mode 0700"):
        delivery.deliver_payload(
            _row(), {"secret": "no"}, delivery_root=root,
            store_path=ledger, now=100.0,
        )
    session_dir.chmod(0o700)
    monkeypatch.setattr(delivery, "SESSION_SECRET_UID", session_dir.stat().st_uid + 1)
    with pytest.raises(delivery.VaultDeliveryError, match="wrong owner"):
        delivery.deliver_payload(
            _row(), {"secret": "still-no"}, delivery_root=root,
            store_path=ledger, now=100.0,
        )

    assert vault_releases.get("release-1", path=ledger) is None
    assert list(session_dir.iterdir()) == []


def test_failed_materialisation_is_unlinked_and_closed_in_ledger(
    tmp_path, monkeypatch,
):
    root = tmp_path / "ramfs"
    session_dir = root / "auto-requester"
    session_dir.mkdir(parents=True)
    session_dir.chmod(0o700)
    ledger = tmp_path / "releases.db"
    monkeypatch.setattr(delivery, "assert_memory_backed", lambda _path: None)
    monkeypatch.setattr(
        delivery,
        "_write_all",
        lambda *_args: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )

    with pytest.raises(OSError, match="synthetic"):
        delivery.deliver_payload(
            _row(),
            {"private_key": "partial-must-disappear"},
            delivery_root=root,
            store_path=ledger,
            now=100.0,
        )

    assert not (session_dir / "vault-open-release-1.json").exists()
    record = vault_releases.get("release-1", path=ledger)
    assert record["shred_reason"] == "delivery_failed"
    assert record["shredded_at"] == 100_000
