"""Tunnel hello — how a dashboard proves it may serve an org's tunnel.

First message on a fresh ``/t/{org}`` WebSocket, TEXT frame. Version 1::

    {
      "v": 1,
      "org": "<org uuid>",
      "signer": "<leaf pub, 64 hex>",
      "ts": <unix seconds>,
      "cert": "<canonical DelegationCert wire JSON>",
      "sig": "<128 hex over TUNNEL_HELLO_DOMAIN ||
               canonical_json({v, org, signer, ts})>"
    }

Version 2 adds the enrolled machine identity and capability negotiation
(auto-0zdky). One canonical unsigned core covers exactly six fields —
``{v, org, signer, machine, caps, ts}`` — and carries two
domain-separated signatures: the serving leaf's under the v2 tunnel
domain, and the enrolled machine key's under the (first-version) machine
domain, proving live custody of that machine key::

    {
      "v": 2,
      "org": "<org uuid>",
      "signer": "<leaf pub, 64 hex>",
      "machine": "<enrolled machine pub, 64 hex>",
      "machine_sig": "<128 hex over MACHINE_HELLO_DOMAIN || core>",
      "caps": ["host-lease/1", ...],        # sorted, unique
      "ts": <unix seconds>,
      "cert": "<canonical DelegationCert wire JSON>",
      "sig": "<128 hex over TUNNEL_HELLO_DOMAIN_V2 || core>"
    }

The registry verifies the cert chain against the org binding's root key
with required scope ``tunnel:serve`` (same I4 discipline as every other
registry mutation — no permission tables), plus ±MAX_CLOCK_SKEW
freshness on ``ts``. The registry answers ``{"ok": true, "v": 1}`` (or
``{"ok": true, "v": 2, "caps": [...accepted...]}``) and the connection
switches to binary mux frames (``frames.py``).
"""

from __future__ import annotations

import json
import re

from tools.network.idkit import DelegationCert, KeyPair, canonical_json

TUNNEL_HELLO_DOMAIN = b"autonomy.network.tunnel.hello.v1\n"
TUNNEL_HELLO_DOMAIN_V2 = b"autonomy.network.tunnel.hello.v2\n"
#: Brand-new domain: its first version is v1 regardless of the hello
#: version whose core it signs (the core itself carries the hello "v").
MACHINE_HELLO_DOMAIN = b"autonomy.network.tunnel.hello.machine.v1\n"
HELLO_VERSION = 1
HELLO_VERSION_2 = 2

HELLO_FIELDS = frozenset({"v", "org", "signer", "ts", "cert", "sig"})
HELLO_FIELDS_V2 = frozenset(
    {"v", "org", "signer", "machine", "machine_sig", "caps", "ts",
     "cert", "sig"}
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_HEX128_RE = re.compile(r"^[0-9a-f]{128}\Z")
_CAP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}/[0-9]{1,4}\Z")
MAX_CAPS = 16


class HelloError(Exception):
    """A tunnel hello that does not parse or verify."""


def hello_signing_input(
    org: str, signer: str, ts: int, *, version: int = HELLO_VERSION
) -> bytes:
    return TUNNEL_HELLO_DOMAIN + canonical_json(
        {"v": version, "org": org, "signer": signer, "ts": ts}
    )


def build_tunnel_hello(key: KeyPair, cert: DelegationCert, *, org: str, ts: int) -> str:
    """Connector side: the signed hello for ``/t/{org}``."""
    if cert.child_pub != key.public_hex:
        raise HelloError("cert does not delegate to the signing key")
    return json.dumps(
        {
            "v": HELLO_VERSION,
            "org": org,
            "signer": key.public_hex,
            "ts": ts,
            "cert": cert.to_json().decode("ascii"),
            "sig": key.sign_hex(hello_signing_input(org, key.public_hex, ts)),
        }
    )


def hello_core(
    *,
    org: str,
    signer: str,
    machine: str,
    caps,
    ts: int,
    version: int = HELLO_VERSION_2,
) -> bytes:
    """The canonical unsigned v2 hello core — exactly six fields, shared
    by both domain-separated signatures. Excludes ``sig``, ``machine_sig``
    and (as in v1) ``cert``, which is authenticated by chain verification."""
    return canonical_json(
        {
            "v": version,
            "org": org,
            "signer": signer,
            "machine": machine,
            "caps": list(caps),
            "ts": ts,
        }
    )


def build_tunnel_hello_v2(
    key: KeyPair,
    cert: DelegationCert,
    *,
    machine_key: KeyPair,
    org: str,
    ts: int,
    caps=(),
) -> str:
    """Connector side: the v2 hello — machine identity + capabilities."""
    if cert.child_pub != key.public_hex:
        raise HelloError("cert does not delegate to the signing key")
    caps_list = sorted({str(cap) for cap in caps})
    core = hello_core(
        org=org,
        signer=key.public_hex,
        machine=machine_key.public_hex,
        caps=caps_list,
        ts=ts,
    )
    return json.dumps(
        {
            "v": HELLO_VERSION_2,
            "org": org,
            "signer": key.public_hex,
            "machine": machine_key.public_hex,
            "machine_sig": machine_key.sign_hex(MACHINE_HELLO_DOMAIN + core),
            "caps": caps_list,
            "ts": ts,
            "cert": cert.to_json().decode("ascii"),
            "sig": key.sign_hex(TUNNEL_HELLO_DOMAIN_V2 + core),
        }
    )


def parse_tunnel_hello(raw, *, allow_version_mismatch: bool = False) -> dict:
    """Structural parse only — chain verification is the registry's job.

    The strict field set is selected by the hello's own ``v``: 1 and 2
    are known shapes; any other version must arrive v1-shaped so an
    authenticated typed version mismatch can still be produced (only
    meaningful with ``allow_version_mismatch``).
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HelloError("hello is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HelloError("hello is not valid JSON") from exc
    if not isinstance(data, dict):
        raise HelloError("hello must be a JSON object")
    if type(data.get("v")) is not int:
        raise HelloError("hello v must be an integer")
    version = data["v"]
    if not allow_version_mismatch and version not in (
        HELLO_VERSION, HELLO_VERSION_2,
    ):
        raise HelloError(f"unsupported hello version: {version!r}")

    if version == HELLO_VERSION_2:
        if set(data) != HELLO_FIELDS_V2:
            raise HelloError(
                f"v2 hello must carry exactly {sorted(HELLO_FIELDS_V2)}"
            )
    else:
        if set(data) != HELLO_FIELDS:
            raise HelloError(
                f"hello must carry exactly {sorted(HELLO_FIELDS)}"
            )
    if type(data["ts"]) is not int:
        raise HelloError("hello ts must be an integer unix timestamp")
    for field in ("org", "signer", "cert", "sig"):
        if not isinstance(data[field], str):
            raise HelloError(f"hello {field} must be a string")
    if version == HELLO_VERSION_2:
        if not isinstance(data["machine"], str) or _HEX64_RE.match(
            data["machine"]
        ) is None:
            raise HelloError("hello machine must be 64 lowercase hex")
        if not isinstance(data["machine_sig"], str) or _HEX128_RE.match(
            data["machine_sig"]
        ) is None:
            raise HelloError("hello machine_sig must be 128 lowercase hex")
        caps = data["caps"]
        if not isinstance(caps, list) or len(caps) > MAX_CAPS:
            raise HelloError(f"hello caps must be a list of ≤{MAX_CAPS}")
        for cap in caps:
            if not isinstance(cap, str) or _CAP_RE.match(cap) is None:
                raise HelloError("hello caps entries must be <name>/<rev>")
        if caps != sorted(set(caps)):
            raise HelloError("hello caps must be sorted and unique")
    return data
