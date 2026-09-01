"""Generate the tunnel hello v2 vectors — fixtures/hello_v2.json.

The seam contract frozen with the dashboard lane (auto-0zdky) requires
cross-implementation-stable bytes for the canonical hello core and both
domain-separated signatures. The fixture is DETERMINISTIC — fixed test
keys, fixed timestamp, Ed25519's deterministic signing — and is built by
CALLING the real hello.py primitives, so it can never drift from the code
it freezes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.hello import (
    MACHINE_HELLO_DOMAIN,
    TUNNEL_HELLO_DOMAIN_V2,
    build_tunnel_hello_v2,
    hello_core,
)

ORG = "11111111-1111-4111-8111-111111111111"
TS = 1_800_000_000
CAPS = ["host-lease/1", "tls-stream/1"]
PERSONA = "ab" * 32

ROOT_PRIV = "11" * 32
SERVE_PRIV = "22" * 32
MACHINE_PRIV = "33" * 32


def build_fixture() -> dict:
    root = KeyPair.from_private_hex(ROOT_PRIV)
    serve = KeyPair.from_private_hex(SERVE_PRIV)
    machine = KeyPair.from_private_hex(MACHINE_PRIV)
    cert = issue_cert(
        root,
        serve.public_hex,
        scope=("tunnel:serve",),
        org=ORG,
        subject=Subject("persona", PERSONA),
        not_before=TS - 100,
        not_after=TS + 30 * 86_400,
    )
    hello = build_tunnel_hello_v2(
        serve, cert, machine_key=machine, org=ORG, ts=TS, caps=CAPS
    )
    core = hello_core(
        org=ORG,
        signer=serve.public_hex,
        machine=machine.public_hex,
        caps=CAPS,
        ts=TS,
    )
    return {
        "description": "tunnel hello v2 golden vectors (auto-0zdky seam r3)",
        "keys": {
            "root_private_hex": ROOT_PRIV,
            "serve_private_hex": SERVE_PRIV,
            "machine_private_hex": MACHINE_PRIV,
            "serve_public_hex": serve.public_hex,
            "machine_public_hex": machine.public_hex,
        },
        "org": ORG,
        "ts": TS,
        "caps": CAPS,
        "persona": PERSONA,
        "domains": {
            "tunnel_v2": TUNNEL_HELLO_DOMAIN_V2.decode("ascii"),
            "machine": MACHINE_HELLO_DOMAIN.decode("ascii"),
        },
        "core_hex": core.hex(),
        "hello": json.loads(hello),
    }


def main() -> int:
    out = Path(__file__).parent / "fixtures" / "hello_v2.json"
    out.write_text(json.dumps(build_fixture(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
