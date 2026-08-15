"""Cross-language acceptance: the browser seals an org root, Python opens it.

The organization root is generated in the operator's browser and never leaves
it in the clear (I1). That is only safe if the sealed payload the browser
produces is genuinely openable by the owner later -- otherwise founding mints
a key nobody can ever recover. This runs the real client ceremony under node
and recovers the root on the Python side from the sealed material alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.graph.schemas.network_identity import ORG_ROOT_ARMOR_PURPOSE
from tools.network.idkit.keys import KeyPair
from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.idkit.sealing import open as seal_open

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js" / "ceremony"
    / "tests" / "seal_org_root_harness.mjs"
)
PERSONAL_SEED = bytes(range(32))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_python_recovers_the_org_root_the_browser_sealed(tmp_path):
    out = tmp_path / "sealed-org-root.json"
    subprocess.run(
        ["node", str(HARNESS), PERSONAL_SEED.hex(), str(out)],
        cwd=REPO_ROOT,
        check=True,
    )
    produced = json.loads(out.read_text())
    sealed = produced["sealed_org_key"]

    # The payload is exactly the shape the store validates -- nothing more.
    assert set(sealed) == {
        "root_pub", "sealed_root_key", "owner_kem_pub", "seal_purpose",
    }
    assert sealed["seal_purpose"] == ORG_ROOT_ARMOR_PURPOSE
    assert sealed["root_pub"] == produced["root_pub"]
    # The same key must sign the founding batch it is generated for.
    assert produced["signature_verifies"] is True

    # The owner derives the same recipient key Python-side, from the personal
    # root seed alone -- no secret travelled with the sealed payload.
    recipient_priv, recipient_pub = derive_encapsulation_keypair(
        PERSONAL_SEED, ORG_ROOT_ARMOR_PURPOSE
    )
    assert recipient_pub == sealed["owner_kem_pub"], (
        "the browser sealed to a different recipient key than the owner derives"
    )

    recovered_seed = seal_open(
        bytes.fromhex(sealed["sealed_root_key"]),
        recipient_priv,
        ORG_ROOT_ARMOR_PURPOSE,
    )
    # The recovered seed IS the organization root: it reproduces its identity.
    assert KeyPair.from_private_hex(recovered_seed.hex()).public_hex == sealed["root_pub"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_a_different_personal_root_cannot_open_it(tmp_path):
    """The seal is to the owner, not to whoever asks."""
    out = tmp_path / "sealed-org-root.json"
    subprocess.run(
        ["node", str(HARNESS), PERSONAL_SEED.hex(), str(out)],
        cwd=REPO_ROOT,
        check=True,
    )
    sealed = json.loads(out.read_text())["sealed_org_key"]

    stranger_priv, _ = derive_encapsulation_keypair(
        bytes(range(1, 33)), ORG_ROOT_ARMOR_PURPOSE
    )
    with pytest.raises(Exception):
        seal_open(
            bytes.fromhex(sealed["sealed_root_key"]),
            stranger_priv,
            ORG_ROOT_ARMOR_PURPOSE,
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_the_seal_does_not_open_under_a_different_purpose(tmp_path):
    """Domain separation: material sealed for the org root opens only as that."""
    out = tmp_path / "sealed-org-root.json"
    subprocess.run(
        ["node", str(HARNESS), PERSONAL_SEED.hex(), str(out)],
        cwd=REPO_ROOT,
        check=True,
    )
    sealed = json.loads(out.read_text())["sealed_org_key"]

    recipient_priv, _ = derive_encapsulation_keypair(
        PERSONAL_SEED, ORG_ROOT_ARMOR_PURPOSE
    )
    with pytest.raises(Exception):
        seal_open(
            bytes.fromhex(sealed["sealed_root_key"]),
            recipient_priv,
            "autonomy/persona-kem/v1/something-else",
        )
