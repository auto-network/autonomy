"""Generate Python settings-envelope vectors consumed by the Node test.

The envelope module in tools/network/settingskit/envelope.py is the wire
authority; ceremony/settings-envelope.js must produce byte-identical canonical
records. settings-envelope.test.mjs consumes this file's output and asserts
equality, per-field byte sensitivity, cross-org non-verification, and the
structural refusals.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.network.idkit import KeyPair, canonical_json, derive_persona
from tools.network.settingskit import (
    build_record,
    record_bytes,
    sign_record,
    signing_input,
)

PERSONAL_ROOT_SEED = bytes(range(32))
WITNESS_SEED = bytes(range(64, 96))
# Fixture realism only: the envelope treats the attestation as opaque, and
# nothing in the tests verifies the witness signature itself.
WITNESS_DOMAIN_V2 = b"autonomy.network.registry.witness.v2\n"

GENESIS_IDS = ["11" * 32, "22" * 32, "33" * 32]
SET_ID = "autonomy.org.member-directory"


def make_attestation(genesis_id: str, t: int) -> dict:
    witness_key = KeyPair.from_private_hex(WITNESS_SEED.hex())
    entry = {
        "v": 2,
        "org": genesis_id,
        "seq": 3,
        "prev": "cc" * 32,
        "t": t,
        "heads": {"authority": sorted(["ee" * 32, "dd" * 32])},
        "publisher": witness_key.public_hex,
    }
    return {
        "entry": entry,
        "entry_id": hashlib.sha256(canonical_json(entry)).hexdigest(),
        "sig": witness_key.sign_hex(WITNESS_DOMAIN_V2 + canonical_json(entry)),
    }


def build_vectors() -> dict:
    personas = [
        derive_persona(PERSONAL_ROOT_SEED, genesis) for genesis in GENESIS_IDS
    ]
    persona_a, persona_b, _ = personas

    payload = {
        "display_name": "Ada ☃ Lovelace 🚀",
        "photo": {"mime": "image/png", "size": 9_007_199_254_740_991},
        "links": [None, True, -7, "𐀀 astral"],
    }
    base_fields = dict(
        org=GENESIS_IDS[0],
        set_id=SET_ID,
        key=persona_a.public_hex,
        schema_revision=1,
        publication_state="published",
        deprecated=False,
        successor_id=None,
        payload=payload,
        signed_at=1_755_500_000_123,
        signing_key=persona_a.public_hex,
        witness=make_attestation(GENESIS_IDS[0], 1_755_500_000),
    )
    base = build_record(**base_fields)
    base_hex = record_bytes(base).hex()

    # One mutation per signed field — ELEVEN. A field that can change without
    # changing the bytes is not signed; the Node test asserts each mutated
    # encoding both matches Python's and differs from the base.
    mutations = {
        "org": {**base_fields, "org": GENESIS_IDS[1]},
        "set_id": {**base_fields, "set_id": SET_ID + ".other"},
        "key": {**base_fields, "key": persona_b.public_hex},
        "schema_revision": {**base_fields, "schema_revision": 2},
        "publication_state": {**base_fields, "publication_state": "canonical"},
        "deprecated": {**base_fields, "deprecated": True},
        "successor_id": {**base_fields, "successor_id": "b2f6f0d6-retraction"},
        "payload": {
            **base_fields,
            "payload": {**payload, "display_name": "Ada ☃ Lovelace"},
        },
        "signed_at": {**base_fields, "signed_at": 1_755_500_000_124},
        "signing_key": {**base_fields, "signing_key": persona_b.public_hex},
        "witness": {
            **base_fields,
            "witness": make_attestation(GENESIS_IDS[0], 1_755_500_001),
        },
    }
    mutation_vectors = {}
    for field, fields in mutations.items():
        mutated_hex = record_bytes(build_record(**fields)).hex()
        assert mutated_hex != base_hex, f"mutating {field} must change the bytes"
        mutation_vectors[field] = {
            "input": build_record(**fields),
            "canonical_hex": mutated_hex,
        }

    no_witness = build_record(**{**base_fields, "witness": None})

    # One member, three organizations: same personal root, one persona per
    # genesis, one signed directory row each.
    cross_org = []
    for genesis, persona in zip(GENESIS_IDS, personas):
        record = build_record(
            org=genesis,
            set_id=SET_ID,
            key=persona.public_hex,
            schema_revision=1,
            publication_state="published",
            deprecated=False,
            successor_id=None,
            payload={"display_name": "The Same Human"},
            signed_at=1_755_500_000_123,
            signing_key=persona.public_hex,
            witness=make_attestation(genesis, 1_755_500_000),
        )
        cross_org.append(
            {
                "genesis_id": genesis,
                "record": record,
                "canonical_hex": record_bytes(record).hex(),
                "signature_hex": sign_record(persona, record),
            }
        )

    # Structural refusals both builders must share, for both key strategies:
    # an envelope that cannot name its signer cannot exist.
    persona_shaped = {k: v for k, v in base_fields.items() if k != "signing_key"}
    delegate_shaped = {
        **{k: v for k, v in base_fields.items() if k != "signing_key"},
        "key": "ops-policy",
    }

    return {
        "personal_root_seed_hex": PERSONAL_ROOT_SEED.hex(),
        "genesis_ids": GENESIS_IDS,
        "base": {
            "input": base,
            "canonical_hex": base_hex,
            "signing_input_hex": signing_input(base).hex(),
            "signature_hex": sign_record(persona_a, base),
        },
        "mutations": mutation_vectors,
        "no_witness": {
            "input": no_witness,
            "canonical_hex": record_bytes(no_witness).hex(),
        },
        # Same payload as the base record, serialized with shuffled key order
        # and whitespace: parsing this text and rebuilding must reproduce the
        # base bytes exactly.
        "payload_json_shuffled": json.dumps(
            payload, ensure_ascii=False, indent=3, sort_keys=True
        ),
        "cross_org": cross_org,
        "refusals": {
            "missing_signing_key_persona": persona_shaped,
            "missing_signing_key_delegate": delegate_shaped,
        },
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: generate_settings_envelope_vectors.py OUTPUT.json"
        )
    output = Path(sys.argv[1])
    output.write_text(
        json.dumps(build_vectors(), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
