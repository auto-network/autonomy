"""Generate Python-ledger vectors consumed by the Node event test."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger.events import make_event
from tools.network.ledger.hlc import HLC

AUTHOR_SEED = bytes(range(32))
PERSONA_ROOT_SEED = bytes(reversed(range(32)))
PARENT_A = "11" * 32
PARENT_B = "22" * 32


def build_vectors() -> dict:
    author = KeyPair.from_private_hex(AUTHOR_SEED.hex())
    invited_persona = KeyPair.from_private_hex(bytes(range(32, 64)).hex())
    payloads = [
        {
            "type": "genesis",
            "org": "00000000-0000-4000-8000-000000000001",
            "root_pub": author.public_hex,
        },
        {
            "type": "role.define",
            "name": "owner",
            "scope_set": ["*"],
            "claim_requires": "self",
            "version": 1,
        },
        {
            "type": "invite",
            "granted_role": "owner",
            "expiry": 1_735_689_600_002,
            "sponsor": author.public_hex,
            "invite_pub": invited_persona.public_hex,
        },
        {
            "type": "member.claim",
            "invite_ref": "33" * 32,
            "persona_pub": invited_persona.public_hex,
            "profile": {"display_name": "Founder 🚀"},
            "approvals": [],
        },
    ]
    vectors = []
    for index, payload in enumerate(payloads):
        input_parents = [] if index == 0 else [PARENT_B, PARENT_A, PARENT_B]
        hlc = HLC(1_735_689_600_000 + index, index)
        event = make_event(author, payload, input_parents, hlc)
        vectors.append(
            {
                "name": payload["type"],
                "input": {
                    "authorKey": author.public_hex,
                    "parents": input_parents,
                    "hlc": hlc.to_list(),
                    "payload": payload,
                },
                "unsigned": event.payload_dict(),
                "signing_input_hex": event.signing_input().hex(),
                "event": event.to_dict(),
                "event_id": event.event_id,
                "wire_hex": event.to_json().hex(),
            }
        )

    genesis_id = vectors[0]["event_id"]
    persona = derive_persona(PERSONA_ROOT_SEED, genesis_id)
    return {
        "author_seed_hex": AUTHOR_SEED.hex(),
        "author_public_hex": author.public_hex,
        "other_public_hex": invited_persona.public_hex,
        "vectors": vectors,
        "persona": {
            "personal_root_seed_hex": PERSONA_ROOT_SEED.hex(),
            "genesis_id": genesis_id,
            "public_hex": persona.public_hex,
        },
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage: generate_ledger_event_vectors.py OUTPUT.json"
        )
    Path(sys.argv[1]).write_text(
        json.dumps(build_vectors(), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
