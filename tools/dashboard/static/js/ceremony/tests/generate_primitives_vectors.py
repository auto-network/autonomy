"""Generate Python idkit vectors consumed by the Node primitives test."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key
from tools.network.idkit.canonical import canonical_json

PASSPHRASE = "pw"
CERT_DOMAIN = "autonomy.idkit.cert.v1\n"


def build_vectors() -> dict:
    seed = bytes(range(32))
    root = KeyPair.from_private_hex(seed.hex())
    canonical_values = [
        {
            "name": "non-bmp-value-and-safe-integer",
            "value": {
                "max_safe_integer": 9_007_199_254_740_991,
                "message": "snowman ☃ and rocket 🚀",
                "nested": [True, None, -7],
            },
        },
        {
            "name": "python-code-point-key-order",
            "value": {
                "\U00010000": "astral key",
                "\ue000": "private-use BMP key",
                "ascii": "first",
            },
        },
    ]
    encoded_values = [
        {
            **vector,
            "canonical_hex": canonical_json(vector["value"]).hex(),
        }
        for vector in canonical_values
    ]
    signing_payload = {
        "issuer": root.public_hex,
        "max_safe_integer": 9_007_199_254_740_991,
        "purpose": "cross-language 🚀",
        "v": 1,
    }
    signing_canonical = canonical_json(signing_payload)
    signing_message = CERT_DOMAIN.encode("ascii") + signing_canonical

    return {
        "seed_hex": seed.hex(),
        "root_pub": root.public_hex,
        "passphrase": PASSPHRASE,
        "armor": encrypt_root_key(
            root,
            PASSPHRASE,
            iterations=10_000,
        ),
        "canonical_vectors": encoded_values,
        "signing": {
            "domain": CERT_DOMAIN,
            "payload": signing_payload,
            "canonical_hex": signing_canonical.hex(),
            "message_hex": signing_message.hex(),
            "python_signature_hex": root.sign(signing_message).hex(),
        },
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: generate_primitives_vectors.py OUTPUT.json"
        )
    output = Path(sys.argv[1])
    output.write_text(
        json.dumps(build_vectors(), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
