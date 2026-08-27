"""A v3 recovery code enrolled in one implementation must open in the other.

The recovery code is printed on one device and typed on another, possibly
years later, and the enrolling side (browser at setup) is often not the opening
side (server or another device at recovery). If the v3 slot format or the
RECOVERY_ARMOR seal disagree by a byte, a correct code cannot recover the
identity — the one failure recovery may never have. Asserted across the Python
↔ JS boundary, both directions, with real crypto.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit import recovery
from tools.network.idkit.keys import KeyPair
from tools.network.idkit.root_factor_policy import (
    add_recovery_slot,
    build_envelope,
    create_password_factor,
    emit_armored_envelope,
    factor_leaf,
    open_root_with_recovery,
    parse_armored_envelope,
    recovery_recipient_public_key,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js" / "ceremony"
    / "tests" / "v3-recovery-parity-harness.mjs"
)


def _node(request: dict) -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS)],
        cwd=REPO_ROOT, input=json.dumps(request),
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_python_enrolled_recovery_slot_opens_in_the_browser():
    root = KeyPair.generate()
    pw, seed = create_password_factor(root.public_hex, "pw.main", "cross-pass-pass", iterations=10_000)
    env = build_envelope(root, generation=1, factors=[pw], access={"pw.main": seed}, policy=factor_leaf("pw.main"))
    code = recovery.generate_recovery_code()
    env = add_recovery_slot(
        env, root_seed=bytes.fromhex(root.private_hex),
        recovery_recipient_pub=recovery_recipient_public_key(code),
        recovery_pub=recovery.derive_recovery_factors(code)["recovery_pub"],
    )
    armor = emit_armored_envelope(env)

    opened = _node({"mode": "open", "armor": armor, "codeHex": code.hex()})
    assert opened["rootPub"] == root.public_hex
    assert opened["privateHex"] == root.private_hex


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_enrolled_recovery_slot_opens_in_python():
    root = KeyPair.generate()
    code = recovery.generate_recovery_code()
    built = _node({
        "mode": "build",
        "rootSeedHex": root.private_hex,
        "rootPub": root.public_hex,
        "codeHex": code.hex(),
    })
    envelope = parse_armored_envelope(built["armor"])
    recovered = open_root_with_recovery(envelope, code)
    assert recovered.public_hex == root.public_hex
    assert recovered.private_hex == root.private_hex
