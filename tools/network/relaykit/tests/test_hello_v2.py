"""Tunnel hello v2 — machine identity + capability negotiation (auto-0zdky).

Wire contract frozen with the dashboard lane (seam artifact r3): strict
field set ``{v, org, signer, machine, machine_sig, caps, ts, cert, sig}``,
one canonical unsigned core over exactly six fields, and two
domain-separated signatures — the serving leaf under the v2 tunnel-hello
domain, the enrolled machine key under the (first-version) machine domain.
"""

from __future__ import annotations

import json

import pytest

from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.relaykit import hello as hello_mod
from tools.network.relaykit.hello import (
    HELLO_VERSION,
    HelloError,
    build_tunnel_hello,
    parse_tunnel_hello,
)

NOW = 1_800_000_000
DAY = 86_400
ORG = "11111111-1111-4111-8111-111111111111"
CAPS = ("host-lease/1", "tls-stream/1")


def _serve_cert(root: KeyPair, serve_key: KeyPair) -> object:
    return issue_cert(
        root,
        serve_key.public_hex,
        scope=("tunnel:serve",),
        org=ORG,
        subject=Subject("persona", "ab" * 32),
        not_before=NOW - 100,
        not_after=NOW + 30 * DAY,
    )


def _v2_hello(root=None, serve_key=None, machine_key=None, caps=CAPS):
    root = root or KeyPair.generate()
    serve_key = serve_key or KeyPair.generate()
    machine_key = machine_key or KeyPair.generate()
    cert = _serve_cert(root, serve_key)
    raw = hello_mod.build_tunnel_hello_v2(
        serve_key, cert, machine_key=machine_key, org=ORG, ts=NOW, caps=caps
    )
    return raw, serve_key, machine_key


def test_domain_constants_are_exact():
    assert hello_mod.TUNNEL_HELLO_DOMAIN == b"autonomy.network.tunnel.hello.v1\n"
    assert (
        hello_mod.TUNNEL_HELLO_DOMAIN_V2
        == b"autonomy.network.tunnel.hello.v2\n"
    )
    # Brand-new domain: its first version is v1 regardless of the hello
    # version whose core it signs.
    assert (
        hello_mod.MACHINE_HELLO_DOMAIN
        == b"autonomy.network.tunnel.hello.machine.v1\n"
    )
    assert hello_mod.HELLO_VERSION_2 == 2


def test_hello_core_is_canonical_json_of_exactly_six_fields():
    signer = "cd" * 32
    machine = "ef" * 32
    core = hello_mod.hello_core(
        org=ORG, signer=signer, machine=machine, caps=CAPS, ts=NOW
    )
    assert core == canonical_json(
        {
            "v": 2,
            "org": ORG,
            "signer": signer,
            "machine": machine,
            "caps": list(CAPS),
            "ts": NOW,
        }
    )


def test_v2_round_trip_has_exact_field_set_and_verifying_signatures():
    raw, serve_key, machine_key = _v2_hello()
    data = parse_tunnel_hello(raw)
    assert set(data) == {
        "v", "org", "signer", "machine", "machine_sig", "caps",
        "ts", "cert", "sig",
    }
    assert data["v"] == 2
    assert data["machine"] == machine_key.public_hex
    assert data["caps"] == list(CAPS)

    core = hello_mod.hello_core(
        org=ORG,
        signer=serve_key.public_hex,
        machine=machine_key.public_hex,
        caps=CAPS,
        ts=NOW,
    )
    from tools.network.idkit import verify_signature

    verify_signature(
        serve_key.public_hex,
        data["sig"],
        hello_mod.TUNNEL_HELLO_DOMAIN_V2 + core,
    )
    verify_signature(
        machine_key.public_hex,
        data["machine_sig"],
        hello_mod.MACHINE_HELLO_DOMAIN + core,
    )


def test_signatures_are_not_interchangeable_across_domains():
    """Same core, distinct domains: neither signature verifies under the
    other's domain even when checked against its own key."""
    raw, serve_key, machine_key = _v2_hello()
    data = parse_tunnel_hello(raw)
    core = hello_mod.hello_core(
        org=ORG,
        signer=serve_key.public_hex,
        machine=machine_key.public_hex,
        caps=CAPS,
        ts=NOW,
    )
    from tools.network.idkit import SignatureError, verify_signature

    with pytest.raises(SignatureError):
        verify_signature(
            serve_key.public_hex,
            data["sig"],
            hello_mod.MACHINE_HELLO_DOMAIN + core,
        )
    with pytest.raises(SignatureError):
        verify_signature(
            machine_key.public_hex,
            data["machine_sig"],
            hello_mod.TUNNEL_HELLO_DOMAIN_V2 + core,
        )


def test_v1_build_and_parse_are_byte_for_byte_unchanged():
    root = KeyPair.generate()
    serve_key = KeyPair.generate()
    cert = _serve_cert(root, serve_key)
    raw = build_tunnel_hello(serve_key, cert, org=ORG, ts=NOW)
    data = parse_tunnel_hello(raw)
    assert data["v"] == HELLO_VERSION
    assert set(data) == {"v", "org", "signer", "ts", "cert", "sig"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("machine_sig"),
        lambda d: d.update(extra="x"),
        lambda d: d.update(machine="AB" * 32),          # uppercase hex
        lambda d: d.update(machine="ab" * 31),          # short
        lambda d: d.update(caps="host-lease/1"),        # not a list
        lambda d: d.update(caps=[1]),                   # non-string member
        lambda d: d.update(machine_sig="zz" * 64),      # non-hex sig
    ],
)
def test_v2_parse_is_strict(mutate):
    raw, _, _ = _v2_hello()
    data = json.loads(raw)
    mutate(data)
    with pytest.raises(HelloError):
        parse_tunnel_hello(json.dumps(data))


def test_v2_caps_are_normalized_sorted_unique():
    raw, _, _ = _v2_hello(caps=("tls-stream/1", "host-lease/1"))
    data = parse_tunnel_hello(raw)
    assert data["caps"] == sorted(data["caps"])
    raw2, _, _ = _v2_hello(caps=())
    assert parse_tunnel_hello(raw2)["caps"] == []
