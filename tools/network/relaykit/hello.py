"""Tunnel hello — how a dashboard proves it may serve an org's tunnel.

First message on a fresh ``/t/{org}`` WebSocket, TEXT frame::

    {
      "v": 1,
      "org": "<org uuid>",
      "signer": "<leaf pub, 64 hex>",
      "ts": <unix seconds>,
      "cert": "<canonical DelegationCert wire JSON>",
      "sig": "<128 hex over TUNNEL_HELLO_DOMAIN ||
               canonical_json({v, org, signer, ts})>"
    }

The registry verifies the cert chain against the org binding's root key
with required scope ``tunnel:serve`` (same I4 discipline as every other
registry mutation — no permission tables), plus ±MAX_CLOCK_SKEW
freshness on ``ts``. The registry answers ``{"ok": true, "v": 1}`` and the
connection switches to binary mux frames (``frames.py``).
"""

from __future__ import annotations

import json

from tools.network.idkit import DelegationCert, KeyPair, canonical_json

TUNNEL_HELLO_DOMAIN = b"autonomy.network.tunnel.hello.v1\n"
HELLO_VERSION = 1

HELLO_FIELDS = frozenset({"v", "org", "signer", "ts", "cert", "sig"})


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


def parse_tunnel_hello(raw, *, allow_version_mismatch: bool = False) -> dict:
    """Structural parse only — chain verification is the registry's job."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HelloError("hello is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HelloError("hello is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != HELLO_FIELDS:
        raise HelloError(f"hello must carry exactly {sorted(HELLO_FIELDS)}")
    if type(data["v"]) is not int:
        raise HelloError("hello v must be an integer")
    if not allow_version_mismatch and data["v"] != HELLO_VERSION:
        raise HelloError(f"unsupported hello version: {data['v']!r}")
    if type(data["ts"]) is not int:
        raise HelloError("hello ts must be an integer unix timestamp")
    for field in ("org", "signer", "cert", "sig"):
        if not isinstance(data[field], str):
            raise HelloError(f"hello {field} must be a string")
    return data
