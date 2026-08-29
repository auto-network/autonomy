"""The browser and the command line must derive the SAME recovery code.

A recovery code is printed by whichever side ran the ceremony and typed into
whichever side is doing the recovering -- often not the same one, and possibly
years apart. If the two derivations differ by a byte, a person holding a
perfectly correct code cannot get back in. That is the one failure a recovery
mechanism may never have, so the agreement is asserted here rather than
assumed from the two implementations looking alike.
"""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit import recovery
from tools.network.idkit.armor import (
    RECOVERY_ARMOR_PURPOSE,
    add_recovery_factor,
    decrypt_root_key_with_recovery,
    encrypt_root_key,
)
from tools.network.idkit.keys import KeyPair
from tools.network.idkit.sealing import derive_encapsulation_keypair

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js" / "ceremony"
    / "tests" / "recovery-parity-harness.mjs"
)
#: Fixed vectors: an ascending code, a descending one, and one that is all
#: high bits, so a byte-order or sign slip on either side shows up.
VECTORS = [
    bytes(range(32)),
    bytes(reversed(range(32))),
    bytes([0xFF] * 32),
]


def _browser(code: bytes) -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), code.hex()],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("code", VECTORS)
def test_both_sides_derive_the_same_recovery_material(code):
    theirs = _browser(code)
    ours = recovery.derive_recovery_factors(code)

    assert ours["recovery_pub"] == theirs["recovery_pub"], (
        "the signing half differs, so a code could not co-sign a key rotation"
    )
    assert ours["kek_recovery_seed"].hex() == theirs["kek_recovery_seed"], (
        "the vault-opening half differs, so a code could not open an armor"
    )
    assert recovery.encode_recovery_code(code) == theirs["printable"], (
        "the printed forms differ, so a person would transcribe the wrong code"
    )
    assert theirs["read_back"] == code.hex()
    assert recovery.decode_recovery_code(theirs["printable"]) == code


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_code_printed_by_the_browser_opens_an_armor_locked_here():
    """The end-to-end claim, across the boundary and in the real direction."""
    code = recovery.generate_recovery_code()
    printable = _browser(code)["printable"]

    # The command line locks an identity, knowing only the code's public half.
    key = KeyPair.generate()
    armor = mint_password_armor(key, "a-password", iterations=10_000)
    kek_seed = recovery.derive_recovery_factors(code)["kek_recovery_seed"]
    _, kem_pub = derive_encapsulation_keypair(kek_seed, RECOVERY_ARMOR_PURPOSE)
    armor = add_recovery_factor(armor, "a-password", kem_pub)

    # A person types in what the browser printed, and gets back in.
    typed = recovery.decode_recovery_code(printable.lower().replace("-", " "))
    assert decrypt_root_key_with_recovery(armor, typed).public_hex == key.public_hex
