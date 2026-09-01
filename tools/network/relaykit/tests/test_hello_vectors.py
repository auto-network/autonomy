"""Hello v2 golden vectors — cross-implementation byte stability.

Rebuilds the fixture's hello from its fixed keys through the real
primitives and requires exact equality: core bytes, both domains, both
signatures (Ed25519 signing is deterministic), and the complete wire
object. A drift in canonical ordering, domain strings, or field sets
fails here before it can strand a deployed connector.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.network.idkit import KeyPair, verify_signature
from tools.network.relaykit.hello import (
    MACHINE_HELLO_DOMAIN,
    TUNNEL_HELLO_DOMAIN_V2,
    hello_core,
    parse_tunnel_hello,
)

from .generate_hello_vectors import build_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "hello_v2.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def test_fixture_matches_regeneration_exactly():
    assert _load() == build_fixture()


def test_domains_are_frozen():
    fixture = _load()
    assert fixture["domains"]["tunnel_v2"] == (
        TUNNEL_HELLO_DOMAIN_V2.decode("ascii")
    )
    assert fixture["domains"]["machine"] == (
        MACHINE_HELLO_DOMAIN.decode("ascii")
    )


def test_core_bytes_and_signatures_verify():
    fixture = _load()
    hello = fixture["hello"]
    core = bytes.fromhex(fixture["core_hex"])
    assert core == hello_core(
        org=fixture["org"],
        signer=fixture["keys"]["serve_public_hex"],
        machine=fixture["keys"]["machine_public_hex"],
        caps=fixture["caps"],
        ts=fixture["ts"],
    )
    verify_signature(
        fixture["keys"]["serve_public_hex"],
        hello["sig"],
        TUNNEL_HELLO_DOMAIN_V2 + core,
    )
    verify_signature(
        fixture["keys"]["machine_public_hex"],
        hello["machine_sig"],
        MACHINE_HELLO_DOMAIN + core,
    )
    # Deterministic signing: the exact signature hex is frozen too.
    serve = KeyPair.from_private_hex(fixture["keys"]["serve_private_hex"])
    machine = KeyPair.from_private_hex(
        fixture["keys"]["machine_private_hex"]
    )
    assert hello["sig"] == serve.sign_hex(TUNNEL_HELLO_DOMAIN_V2 + core)
    assert hello["machine_sig"] == machine.sign_hex(
        MACHINE_HELLO_DOMAIN + core
    )


def test_fixture_hello_parses_strictly():
    data = parse_tunnel_hello(json.dumps(_load()["hello"]))
    assert data["v"] == 2
    assert data["caps"] == ["host-lease/1", "tls-stream/1"]
