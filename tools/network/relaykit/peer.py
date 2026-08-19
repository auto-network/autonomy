"""Peer relay — an org member's reachable node carrying org traffic (G1).

Spec `graph://eb245082-b76` §8: the ``relay:serve`` delegation turns a
member's public node into part of the org's own relay fleet — the middle
rung of the connectivity fallback chain (direct → peer relay →
auto.network floor). Two facts define it:

1. **Authority is a delegation, not configuration.** The relay proves it
   may serve by signing a client-chosen nonce with a ``relay:serve``-
   scoped idkit chain to the org root. Every client — dialer and parking
   node alike — verifies that chain against its own pinned root before a
   single channel byte flows; a node whose chain lacks the scope is
   refused as a relay by construction. The same grant is recorded in the
   org authority ledger as a ``delegate`` event carrying the
   ``relay:serve`` scope, which is how peers *discover* who may relay
   (``dialer.relay_candidates``): the ledger names them, the chain
   proves it on the wire, and revoking the ledger delegation is the org's
   signal to stop parking there and to stop minting the cert at renewal.
2. **It carries ciphertext only.** The relay speaks the B2 mux
   (``frames.py``) and never parses past the frame header — same I5
   posture as the central relay, pinned by the frame-scan test. What it
   can observe is the peer-relay metadata set: who dials whom, session
   ids, timing, volume.

Wire protocol (all control messages TEXT, channel bytes BINARY):

``/t/{org}`` — a node parks a serve-tunnel (outbound, B2-style)::

    parker → relay   {"v": 1, "purpose": "park", "nonce": <32 hex>}
    relay  → parker  RELAY_HELLO (signed over purpose+nonce; the proof)
    parker → relay   tunnel hello (hello.py — tunnel:serve chain)
    relay  → parker  {"ok": true}
    ... binary mux frames, exactly the B2 tunnel contract ...

Parked tunnels are keyed by the hello signer's public key: **the node's
key is its address**. A re-park under the same key replaces the old
tunnel (heals half-dead TCP, same as the central relay).

``/dial/{org}`` — a dialer asks for a bridge to a parked node::

    dialer → relay   {"v": 1, "purpose": "dial", "nonce": <32 hex>,
                      "target": <64 hex node pub>, "session": <32 hex>}
    relay  → dialer  RELAY_HELLO (signed over purpose+nonce+target+session)
    relay  → dialer  {"ok": true}    # or close 4404: target not parked
    ... binary bytes ↔ DATA frames on the target's tunnel ...

The dialer then runs the standard E2E channel handshake through the
pipe with ``token = session`` — the relay never holds channel keys, and
a relay substituting ECDH material fails the pin exactly like the
central relay (I5).

v1 boundaries, deliberate:

- The relay checks chain validity and expiry but holds no revocation
  denylist (that lives in the registry store); a revoked-but-unexpired
  tunnel cert is caught at the registry floor and by cert renewal.
- The dial side is unauthenticated — the same anonymous-client posture
  as the central relay's viewer endpoint (rung-1, Tier A on-ramp). What
  an anonymous dialer can reach is the parked node's ``handler`` seam,
  whose application-layer grant checks (C4/I9) are the authorization
  boundary; certs presented in hellos are signed public statements, not
  secrets. Requiring dialers to prove org membership at the relay is a
  rung-2 addition, orthogonal to the chain built here.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import secrets
import time
from typing import Dict, Optional

import websockets

from tools.network.idkit import (
    DelegationCert,
    IdkitError,
    KeyPair,
    canonical_json,
    verify_chain,
    verify_signature,
)

from .connector import TunnelConnector, echo_handler
from .frames import (
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    FrameError,
    decode_frame,
    encode_frame,
    new_channel_id,
)
from .hello import HELLO_VERSION, HelloError, hello_signing_input, parse_tunnel_hello

RELAY_HELLO_DOMAIN = b"autonomy.network.relay.hello.v1\n"
RELAY_HELLO_VERSION = 1

#: Maximum tolerated |verifier now - relay hello ts|, seconds. Owned by
#: tools.network.clock, which both relaykit and registry import — that shared
#: home is what keeps this equal to the registry envelope skew without
#: relaykit ever importing registry.
from tools.network.clock import MAX_RELAY_SKEW

NONCE_HEX_LEN = 32
SESSION_HEX_LEN = 32

#: Application close codes (mirror the central relay's).
CLOSE_UNAUTHENTICATED = 4403
CLOSE_UNKNOWN_TARGET = 4404
CLOSE_REPLACED = 4409

_HELLO_FIELDS = frozenset({"v", "org", "signer", "ts", "cert", "sig"})


class RelayVerifyError(Exception):
    """The relay's relay:serve proof is missing, malformed, or forged."""


def _require_hex(value: object, length: int, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or value != value.lower()
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise RelayVerifyError(f"{what} must be {length} lowercase hex chars")
    return value


def relay_hello_signing_input(org: str, purpose: str, nonce: str, signer: str,
                              ts: int, **binding) -> bytes:
    """The bytes a RELAY_HELLO signature covers.

    *binding* carries the dial request's target+session so a captured
    hello can never be replayed against a different bridge request; the
    park probe binds only the nonce.
    """
    doc = {"v": RELAY_HELLO_VERSION, "org": org, "purpose": purpose,
           "nonce": nonce, "signer": signer, "ts": ts}
    if set(binding) & set(doc):
        raise RelayVerifyError("binding fields cannot shadow hello fields")
    doc.update(binding)
    return RELAY_HELLO_DOMAIN + canonical_json(doc)


def build_relay_hello(key: KeyPair, cert: DelegationCert, *, org: str,
                      purpose: str, nonce: str, ts: int, **binding) -> str:
    """Relay side: the signed relay:serve proof for one client challenge."""
    if cert.child_pub != key.public_hex:
        raise RelayVerifyError("cert does not delegate to the relay signing key")
    return json.dumps(
        {
            "v": RELAY_HELLO_VERSION,
            "org": org,
            "signer": key.public_hex,
            "ts": ts,
            "cert": cert.to_json().decode("ascii"),
            "sig": key.sign_hex(relay_hello_signing_input(
                org, purpose, nonce, key.public_hex, ts, **binding
            )),
        }
    )


def verify_relay_hello(
    raw,
    *,
    root_pub: str,
    org: str,
    purpose: str,
    nonce: str,
    now: Optional[int] = None,
    **binding,
) -> str:
    """Client side: the gate that makes relay:serve a delegation, not a
    configuration entry.

    Verifies, in order: structure; ``ts`` freshness; the cert chains to
    *root_pub* for *org* with scope ``relay:serve``; the signature (over
    the client's own *nonce* plus the dial *binding*) verifies against
    the chain's leaf. Raises :class:`RelayVerifyError` on any failure —
    after which the caller must refuse the relay. Returns the relay's
    leaf public key.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RelayVerifyError("relay hello is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RelayVerifyError("relay hello is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != _HELLO_FIELDS:
        raise RelayVerifyError(f"relay hello must carry exactly {sorted(_HELLO_FIELDS)}")
    if data["v"] != RELAY_HELLO_VERSION:
        raise RelayVerifyError(f"unsupported relay hello version: {data['v']!r}")
    if data["org"] != org:
        raise RelayVerifyError("relay hello org mismatch")
    if type(data["ts"]) is not int:
        raise RelayVerifyError("relay hello ts must be an integer unix timestamp")
    for field in ("signer", "cert", "sig"):
        if not isinstance(data[field], str):
            raise RelayVerifyError(f"relay hello {field} must be a string")
    now = int(time.time()) if now is None else now
    if abs(now - data["ts"]) > MAX_RELAY_SKEW:
        raise RelayVerifyError(f"relay hello ts outside ±{MAX_RELAY_SKEW}s window")

    try:
        cert = DelegationCert.from_json(data["cert"])
        if cert.child_pub != data["signer"]:
            raise RelayVerifyError("cert does not delegate to the relay hello signer")
        verify_chain(cert, root_pub, org=org, now=now, required_scope="relay:serve")
        verify_signature(
            data["signer"],
            data["sig"],
            relay_hello_signing_input(
                org, purpose, nonce, data["signer"], data["ts"], **binding
            ),
        )
    except IdkitError as exc:
        raise RelayVerifyError(f"{type(exc).__name__}: {exc}") from exc
    return data["signer"]


async def _recv_text(ws, what: str) -> str:
    message = await ws.recv()
    if isinstance(message, (bytes, bytearray)):
        raise RelayVerifyError(f"expected TEXT {what}")
    return message


def _parse_control(raw: str, purpose: str, fields: frozenset) -> dict:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RelayVerifyError("control message is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != fields:
        raise RelayVerifyError(f"control message must carry exactly {sorted(fields)}")
    if data["v"] != RELAY_HELLO_VERSION or data["purpose"] != purpose:
        raise RelayVerifyError("control message version/purpose mismatch")
    _require_hex(data["nonce"], NONCE_HEX_LEN, "nonce")
    return data


class _ParkedTunnel:
    """One node's serve-tunnel plus its open bridged channels."""

    def __init__(self, ws, node_pub: str):
        self.ws = ws
        self.node_pub = node_pub
        self.channels: Dict[bytes, object] = {}  # channel_id -> dialer ws
        self._send_lock = asyncio.Lock()

    async def send_frame(self, frame_type: int, channel_id: bytes,
                         payload: bytes = b"") -> None:
        async with self._send_lock:
            await self.ws.send(encode_frame(frame_type, channel_id, payload))


class PeerRelay:
    """A member node's relay service: park tunnels, bridge dialers.

    *key*/*cert* are this node's ``relay:serve`` delegation — what it
    proves to every client. *root_pub* is the org root it verifies
    parked nodes' ``tunnel:serve`` hellos against (the relay is itself
    an org member; the root is its pin, not a permission table).
    """

    def __init__(self, org: str, root_pub: str, key: KeyPair,
                 cert: DelegationCert, *, host: str = "127.0.0.1",
                 port: int = 0, now_fn=None):
        self._org = org
        self._root_pub = root_pub
        self._key = key
        self._cert = cert
        self._host = host
        self._port = port
        self._now = now_fn or (lambda: int(time.time()))
        self._server = None
        self._tunnels: Dict[str, _ParkedTunnel] = {}  # node pub -> tunnel

    @property
    def port(self) -> int:
        return self._port

    def parked_nodes(self) -> list:
        return sorted(self._tunnels)

    async def start(self) -> int:
        self._server = await websockets.serve(
            self._handle, self._host, self._port, max_size=2**22, compression=None
        )
        self._port = self._server.sockets[0].getsockname()[1]
        return self._port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _observe_data(self, payload: bytes) -> None:
        """Every channel byte this process bridges passes through here.

        Production no-op; the blindness acceptance test hooks it to scan
        exactly what the relaying node's process can see (ciphertext
        records only — I5 extended to the peer rung)."""

    async def _bridge_to_dialer(self, dialer, channel_id: bytes,
                                payload: bytes) -> None:
        """Forward one channel message node → dialer. Split out (with its
        mirror below) so the test suite can subclass the relay into an
        active adversary — the I5 claim is only meaningful against a relay
        that CAN tamper and provably gains nothing."""
        self._observe_data(payload)
        await dialer.send(payload)

    async def _bridge_to_node(self, tunnel: "_ParkedTunnel", channel_id: bytes,
                              payload: bytes) -> None:
        """Forward one channel message dialer → node."""
        self._observe_data(payload)
        await tunnel.send_frame(FRAME_DATA, channel_id, payload)

    async def _handle(self, ws) -> None:
        path = ws.request.path
        try:
            if path == f"/t/{self._org}":
                await self._handle_park(ws)
            elif path == f"/dial/{self._org}":
                await self._handle_dial(ws)
            else:
                await ws.close(code=CLOSE_UNKNOWN_TARGET)
        except websockets.exceptions.ConnectionClosed:
            pass
        except RelayVerifyError:
            with contextlib.suppress(Exception):
                await ws.close(code=CLOSE_UNAUTHENTICATED)

    # -- park side: a node's serve-tunnel ---------------------------------

    async def _handle_park(self, ws) -> None:
        probe = _parse_control(
            await _recv_text(ws, "park probe"), "park",
            frozenset({"v", "purpose", "nonce"}),
        )
        await ws.send(build_relay_hello(
            self._key, self._cert, org=self._org, purpose="park",
            nonce=probe["nonce"], ts=self._now(),
        ))

        raw_hello = await _recv_text(ws, "tunnel hello")
        try:
            node_pub = self._verify_tunnel_hello(raw_hello)
        except HelloError as exc:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"ok": False, "error": str(exc)}))
            await ws.close(code=CLOSE_UNAUTHENTICATED)
            return

        tunnel = _ParkedTunnel(ws, node_pub)
        replaced = self._tunnels.get(node_pub)
        self._tunnels[node_pub] = tunnel
        if replaced is not None:
            with contextlib.suppress(Exception):
                await replaced.ws.close(code=CLOSE_REPLACED)
        await ws.send(json.dumps({"ok": True, "v": HELLO_VERSION}))

        try:
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                try:
                    frame = decode_frame(raw)
                except FrameError:
                    break
                dialer = tunnel.channels.get(frame.channel_id)
                if dialer is None:
                    continue
                if frame.type == FRAME_DATA:
                    try:
                        await self._bridge_to_dialer(dialer, frame.channel_id,
                                                     frame.payload)
                    except Exception:
                        tunnel.channels.pop(frame.channel_id, None)
                elif frame.type == FRAME_CLOSE:
                    tunnel.channels.pop(frame.channel_id, None)
                    with contextlib.suppress(Exception):
                        await dialer.close(code=1000)
        finally:
            if self._tunnels.get(node_pub) is tunnel:
                del self._tunnels[node_pub]
            for dialer in list(tunnel.channels.values()):
                with contextlib.suppress(Exception):
                    await dialer.close(code=1001)
            tunnel.channels.clear()

    def _verify_tunnel_hello(self, raw: str) -> str:
        """The park gate: tunnel:serve chain to the pinned org root."""
        data = parse_tunnel_hello(raw)
        now = self._now()
        if data["org"] != self._org:
            raise HelloError("hello org does not match relay org")
        if abs(now - data["ts"]) > MAX_RELAY_SKEW:
            raise HelloError(f"hello ts outside ±{MAX_RELAY_SKEW}s freshness window")
        try:
            verify_signature(data["signer"], data["sig"],
                             hello_signing_input(data["org"], data["signer"], data["ts"]))
            cert = DelegationCert.from_json(data["cert"])
            if cert.child_pub != data["signer"]:
                raise HelloError("cert does not delegate to the hello signer")
            verify_chain(cert, self._root_pub, org=self._org, now=now,
                         required_scope="tunnel:serve")
        except IdkitError as exc:
            raise HelloError(f"{type(exc).__name__}: {exc}") from exc
        return data["signer"]

    # -- dial side: bridge a dialer to a parked node ----------------------

    async def _handle_dial(self, ws) -> None:
        request = _parse_control(
            await _recv_text(ws, "dial request"), "dial",
            frozenset({"v", "purpose", "nonce", "target", "session"}),
        )
        target = _require_hex(request["target"], 64, "target")
        session = _require_hex(request["session"], SESSION_HEX_LEN, "session")
        await ws.send(build_relay_hello(
            self._key, self._cert, org=self._org, purpose="dial",
            nonce=request["nonce"], ts=self._now(),
            target=target, session=session,
        ))

        tunnel = self._tunnels.get(target)
        if tunnel is None:
            await ws.close(code=CLOSE_UNKNOWN_TARGET)
            return
        await ws.send(json.dumps({"ok": True}))

        channel_id = new_channel_id()
        tunnel.channels[channel_id] = ws
        try:
            await tunnel.send_frame(FRAME_OPEN, channel_id,
                                    canonical_json({"token": session}))
        except Exception:
            tunnel.channels.pop(channel_id, None)
            await ws.close(code=CLOSE_UNKNOWN_TARGET)
            return

        try:
            async for raw in ws:
                if isinstance(raw, str):
                    continue
                if tunnel.channels.get(channel_id) is not ws:
                    break  # torn down from the parked node's side
                try:
                    await self._bridge_to_node(tunnel, channel_id, bytes(raw))
                except Exception:
                    break
        finally:
            if tunnel.channels.pop(channel_id, None) is not None:
                with contextlib.suppress(Exception):
                    await tunnel.send_frame(FRAME_CLOSE, channel_id)


class PeerParkConnector(TunnelConnector):
    """A node's outbound serve-tunnel to a PEER relay.

    Identical to the B2 connector except for the handshake: before
    presenting its own tunnel hello, the parker challenges the relay for
    a fresh ``relay:serve`` proof against the pinned org root. A node
    without the delegation is refused as a relay by its would-be
    parkers, not just by dialers.
    """

    def __init__(self, relay_url: str, org: str, key: KeyPair,
                 cert: DelegationCert, handler=echo_handler, *,
                 root_pub: str, **kwargs):
        super().__init__(relay_url, org, key, cert, handler, **kwargs)
        self._root_pub = root_pub

    async def _handshake(self, ws) -> None:
        nonce = secrets.token_hex(NONCE_HEX_LEN // 2)
        await ws.send(json.dumps(
            {"v": RELAY_HELLO_VERSION, "purpose": "park", "nonce": nonce}
        ))
        verify_relay_hello(
            await ws.recv(), root_pub=self._root_pub, org=self._org,
            purpose="park", nonce=nonce,
        )
        await super()._handshake(ws)


def main() -> None:
    parser = argparse.ArgumentParser(description="auto.network peer relay")
    parser.add_argument("--org", required=True)
    parser.add_argument("--root-pub", required=True, help="org root public key, 64 hex")
    parser.add_argument("--key-file", required=True, help="file holding the private key hex")
    parser.add_argument("--cert-file", required=True,
                        help="file holding the relay:serve cert wire JSON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())

    relay = PeerRelay(args.org, args.root_pub, key, cert,
                      host=args.host, port=args.port)

    async def run():
        await relay.start()
        await asyncio.Event().wait()  # serve until killed

    asyncio.run(run())


if __name__ == "__main__":
    main()
