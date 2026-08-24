"""Root-reachable policy classes: one stable anchor behind personal root."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit.keys import KeyPair
from tools.vault import service
from tools.vault.errors import ClassOpenError, VaultError
from tools.vault.policy_class import (
    PolicyClassRecord,
    governance_policy,
    is_root_reachable,
    open_cek,
    seal_cek,
)
from tools.vault.root_anchor import (
    RootAnchorRecord,
    create_root_anchor,
    open_root_anchor,
)
from tools.vault.store import VaultStore


def _world():
    root = KeyPair.generate()
    anchor, seed = create_root_anchor(
        root,
        anchor_id="personal-root-default",
        display_name="Personal root vault access",
        created_at="2026-08-24T00:00:00Z",
        anchor_seed=bytes(range(32)),
    )
    store = VaultStore()
    service.enroll_root_anchor(store, anchor.to_dict())
    class_id = service.create_root_policy_class(
        store,
        anchor.anchor_id,
        display_name="Personal root vault",
        created_at="2026-08-24T00:00:01Z",
    )
    return root, anchor, seed, store, store.get_class(class_id)


def test_root_gesture_opens_anchor_then_class_and_cek():
    root, anchor, anchor_seed, store, record = _world()
    try:
        assert is_root_reachable(record)
        assert record.policy == governance_policy(record.governance)
        assert record.factor_ids() == (anchor.anchor_id,)
        opened_anchor = open_root_anchor(
            anchor, bytes.fromhex(root.private_hex),
        )
        assert opened_anchor == anchor_seed

        cek = bytes(reversed(range(32)))
        sealed = seal_cek(
            record,
            cek,
            genesis_id="genesis-personal",
            setting_name="mac.ssh.private-key",
            required_policy=record.policy,
        )
        assert open_cek(
            record,
            {anchor.anchor_id: opened_anchor},
            sealed,
            genesis_id="genesis-personal",
            setting_name="mac.ssh.private-key",
            required_policy=record.policy,
        ) == cek
    finally:
        store.close()


def test_root_class_neither_enumerates_nor_accepts_root_armor_factors():
    _root, anchor, anchor_seed, store, record = _world()
    try:
        assert record.governance == {
            "v": 1,
            "form": "root-reachable",
            "anchor_id": anchor.anchor_id,
            "display_name": "Personal root vault",
        }
        with pytest.raises(ClassOpenError, match="anchor was not opened"):
            from tools.vault.policy_class import open_class
            open_class(record, {"some-password": anchor_seed})
    finally:
        store.close()


def test_wrong_root_cannot_open_anchor_and_record_tampering_is_detected():
    _root, anchor, _seed, store, _record = _world()
    try:
        with pytest.raises(VaultError, match="does not open this vault anchor"):
            open_root_anchor(anchor, bytes.fromhex(KeyPair.generate().private_hex))
        tampered = {**anchor.to_dict(), "display_name": "Attacker anchor"}
        with pytest.raises(VaultError, match="not signed"):
            RootAnchorRecord.from_dict(tampered)
    finally:
        store.close()


def test_anchor_enrollment_is_idempotent_but_not_replaceable():
    _root, anchor, _seed, store, _record = _world()
    try:
        store.put_root_anchor(anchor)
        replacement = dataclasses.replace(anchor, display_name="Replacement")
        with pytest.raises(VaultError, match="cannot be replaced"):
            store.put_root_anchor(replacement)
    finally:
        store.close()


def test_root_class_wire_round_trip_keeps_governance_commitment():
    _root, _anchor, _seed, store, record = _world()
    try:
        decoded = PolicyClassRecord.from_dict(record.to_dict())
        assert decoded == record
        forged = {**record.to_dict(), "policy": "password"}
        with pytest.raises(VaultError, match="governance commitment"):
            PolicyClassRecord.from_dict(forged)
    finally:
        store.close()


def _node(value: dict) -> dict:
    runner = (
        Path(__file__).resolve().parents[2]
        / "dashboard/static/js/ceremony/tests/root-anchor-runner.mjs"
    )
    result = subprocess.run(
        ["node", str(runner)],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_python_anchor_opens_byte_identically_in_browser_crypto():
    root, anchor, seed, store, _record = _world()
    try:
        result = _node({
            "action": "open",
            "root_seed": root.private_hex,
            "anchor": anchor.to_dict(),
        })
        assert result["anchor_seed"] == seed.hex()
    finally:
        store.close()


def test_browser_anchor_is_accepted_and_opened_by_python():
    root = KeyPair.generate()
    seed = bytes(reversed(range(32)))
    result = _node({
        "action": "create",
        "root_seed": root.private_hex,
        "root_pub": root.public_hex,
        "anchor_id": "browser-root-default",
        "display_name": "Browser-created personal anchor",
        "created_at": "2026-08-24T00:00:00Z",
        "anchor_seed": seed.hex(),
    })
    anchor = RootAnchorRecord.from_dict(result["anchor"])
    assert open_root_anchor(anchor, bytes.fromhex(root.private_hex)) == seed


def test_browser_refuses_a_store_substituted_anchor_before_decrypting():
    root, anchor, _seed, store, _record = _world()
    try:
        forged = {**anchor.to_dict(), "display_name": "Substituted anchor"}
        runner = (
            Path(__file__).resolve().parents[2]
            / "dashboard/static/js/ceremony/tests/root-anchor-runner.mjs"
        )
        result = subprocess.run(
            ["node", str(runner)],
            input=json.dumps({
                "action": "open",
                "root_seed": root.private_hex,
                "anchor": forged,
            }),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "not signed by this personal root" in result.stderr
    finally:
        store.close()
