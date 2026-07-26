"""Cross-language acceptance for revision-2 organization-root sealing."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit import (
    derive_encapsulation_keypair,
    seal,
    seal_open,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
CEREMONY_DIR = REPO_ROOT / "tools/dashboard/static/js/ceremony"
VECTOR_PATH = CEREMONY_DIR / "tests/sealing-vector.json"
NODE_TEST = CEREMONY_DIR / "tests/sealing.test.mjs"
VENDOR_PATH = (
    REPO_ROOT
    / "tools/dashboard/static/vendor"
    / "hpke-x25519-chacha20poly1305-1.8.0.mjs"
)
VENDOR_SHA256 = "621ad61d026f526711ad0842b03ab92d735a96c1f173a62005f196bb4ecac37d"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_js_hpke_matches_python_in_both_directions(tmp_path: Path) -> None:
    """Python and the real vendored JS implementation share one wire format."""

    vector = json.loads(VECTOR_PATH.read_text())
    seed = bytes.fromhex(vector["seed_hex"])
    purpose = vector["purpose"]

    private_hex, public_hex = derive_encapsulation_keypair(seed, purpose)
    assert private_hex == vector["private_key_hex"]
    assert public_hex == vector["public_key_hex"]
    assert (
        seal_open(
            bytes.fromhex(vector["record_hex"]),
            private_hex,
            purpose,
        ).hex()
        == vector["plaintext_hex"]
    )

    plaintext = bytes(range(32))
    fixture = {
        "python_record_hex": seal(plaintext, public_hex, purpose).hex(),
        "plaintext_hex": plaintext.hex(),
    }
    fixture_path = tmp_path / "python-sealing-fixture.json"
    fixture_path.write_text(json.dumps(fixture))

    completed = subprocess.run(
        ["node", str(NODE_TEST), str(fixture_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["opened_python_hex"] == plaintext.hex()
    assert (
        seal_open(
            bytes.fromhex(result["js_record_hex"]),
            private_hex,
            purpose,
        )
        == plaintext
    )


def test_vendored_hpke_bundle_is_the_reviewed_build() -> None:
    """Pin the exact audited-library build used by browser and Node."""

    assert hashlib.sha256(VENDOR_PATH.read_bytes()).hexdigest() == VENDOR_SHA256
