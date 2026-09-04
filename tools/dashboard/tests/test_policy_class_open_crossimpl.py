"""The browser openContentKey opens a Python-sealed CEK — for EVERY policy
variant real vault data uses.

This replaces a hand-inlined password-only vector that shipped B-1 with a
false-green: it omitted wrap ``factor_type`` and never exercised the
root-reachable anchor path, so the browser open failed live on the operator's
SSH keys (all sealed to a root-reachable class). Here the vectors are generated
from the real Python seal (tools.vault.policy_class) and driven through the real
JS ``openContentKey`` — the actual production function, across password AND
root-reachable — so a derivation/purpose drift in either branch fails loudly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit import KeyPair
from tools.vault import PASSWORD_POLICY, policy_class as pc
from tools.vault.factors import create_password_factor, open_password_seed
from tools.vault.root_anchor import create_root_anchor


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/tests/policy-class-open-vector.mjs"
)
GENESIS = "cb" * 32
SETTING = "91" * 32
AT = "2026-08-27T00:00:00Z"


def _bundle(record, sealed):
    gen = record.current()
    return {
        "class_id": record.class_id,
        "policy": record.policy,
        "genesis_id": GENESIS,
        "setting_name": SETTING,
        "generation": gen.to_dict(),
        "sealed_cek": sealed,
    }


def _password_vector():
    factor = create_password_factor("hunter2", factor_id="operator-password")
    root = KeyPair.from_private_hex(bytes(range(32)).hex())
    anchor, _seed = create_root_anchor(
        root, anchor_id="personal-root-default", display_name="t", created_at=AT,
    )
    record = pc.create_class(
        PASSWORD_POLICY, [factor.published],
        recovery=anchor.published_recipient(), created_at=AT,
    )
    record = pc.enable_public_sealing(record, created_at=AT)
    cek = bytes(range(32))
    sealed = pc.seal_cek(
        record, cek, genesis_id=GENESIS, setting_name=SETTING,
        required_policy=record.policy,
    )
    seed = open_password_seed(factor.armor, "hunter2")
    return record, sealed, cek, {factor.published.factor_id: seed.hex()}


def _root_reachable_vector():
    root = KeyPair.from_private_hex(bytes(range(1, 33)).hex())
    anchor, anchor_seed = create_root_anchor(
        root, anchor_id="personal-root-default", display_name="t", created_at=AT,
    )
    record = pc.create_root_reachable_class(
        anchor.published_recipient(), display_name="v", created_at=AT,
    )
    cek = bytes(range(32, 64))
    sealed = pc.seal_cek(
        record, cek, genesis_id=GENESIS, setting_name=SETTING,
        required_policy=record.policy,
    )
    fid = anchor.published_recipient().factor_id
    return record, sealed, cek, {fid: anchor_seed.hex()}


def _run(tmp_path, bundle, openers, name):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"bundle": bundle, "openers": openers}), "utf-8")
    result = subprocess.run(
        ["node", str(NODE_DRIVER), str(path)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_password_class_opens_in_the_browser(tmp_path):
    record, sealed, cek, openers = _password_vector()
    out = _run(tmp_path, _bundle(record, sealed), openers, "pw")
    assert out["ok"] is True, out
    assert out["cek"] == cek.hex()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_root_reachable_class_opens_in_the_browser(tmp_path):
    """The regression B-1 shipped: the anchor wrap derives under the policy-
    recipient purpose, not the factor purpose."""
    record, sealed, cek, openers = _root_reachable_vector()
    out = _run(tmp_path, _bundle(record, sealed), openers, "rr")
    assert out["ok"] is True, out
    assert out["cek"] == cek.hex()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_wrong_opener_fails_closed(tmp_path):
    record, sealed, _cek, _openers = _password_vector()
    out = _run(
        tmp_path, _bundle(record, sealed),
        {"operator-password": "00" * 32}, "pw-wrong",
    )
    assert out["ok"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_two_of_two_policy_is_refused(tmp_path):
    record, sealed, _cek, openers = _password_vector()
    bundle = _bundle(record, sealed)
    bundle["policy"] = "both"  # the browser refuses 2-of-2 (out of phase-1 scope)
    out = _run(tmp_path, bundle, openers, "both")
    assert out["ok"] is False
    assert "two-of-two" in out["error"]
