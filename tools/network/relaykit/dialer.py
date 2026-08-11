"""The connectivity fallback chain — how two org endpoints get connected.

Spec `graph://eb245082-b76` §8, bead G1: **direct → org peer relay →
auto.network floor.** The org absorbs load; the center provides the
floor, not the ceiling.

1. **Direct** — race the target's candidate addresses from the
   registry's reachability hints; short per-attempt timeout.
2. **Peer relay** — an org member's reachable node bridging the two
   ends. Candidates come from the ledger ∩ hints
   (:func:`relay_candidates`): the authority ledger says who holds the
   ``relay:serve`` delegation, the hints say where to dial them. Before
   any channel byte flows the dialer challenges the relay for a signed
   ``relay:serve`` proof against the pinned org root
   (``peer.verify_relay_hello``) — a node without the delegation is
   refused *by the client*, chain verification, not configuration.
3. **Floor** — the central relay's viewer channel (B2), which always
   ultimately succeeds while the org's own fabric is degraded.

Every rung carries the identical E2E channel (I5): the transport is
untrusted everywhere, so falling down the chain trades latency and
metadata exposure, never confidentiality or integrity.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Optional

import websockets

from .channel import (
    ChannelCrypto,
    HandshakeError,
    build_client_hello,
    verify_server_hello,
)
from .direct import direct_connect, new_session_id
from .peer import NONCE_HEX_LEN, RELAY_HELLO_VERSION, verify_relay_hello
from .viewer import ViewerChannel, read_viewer_record

PATH_DIRECT = "direct"
PATH_PEER_RELAY = "peer-relay"
PATH_FLOOR = "floor"


class DialError(Exception):
    """Every rung of the fallback chain failed."""

    def __init__(self, attempts: list):
        self.attempts = attempts
        super().__init__(
            "all connectivity paths failed: "
            + "; ".join(f"[{p}] {e}: {err}" for p, e, err in attempts)
        )


@dataclass
class DialResult:
    """An established channel plus how the chain got there."""

    channel: ViewerChannel
    path: str  # PATH_DIRECT | PATH_PEER_RELAY | PATH_FLOOR
    via: str   # the address / relay URL / registry URL that carried it
    #: (path, endpoint, error) for every rung tried before this one
    attempts: list = field(default_factory=list)


def relay_candidates(live_keys: dict, hints: list) -> list:
    """Ledger ∩ hints: dialable peer relays for an org.

    *live_keys* is the ledger's ``live-keys`` projection body (key →
    scope patterns it currently holds — the deterministic fold's
    output); *hints* are registry reachability rows. A node is a
    candidate iff the ledger says its key holds ``relay:serve`` AND it
    advertises a relay dial URL. The intersection is discovery only —
    enforcement is the on-wire chain check in ``verify_relay_hello``;
    a hint row whose key the ledger does not authorize is simply never
    dialed, and one that lied would fail the chain check anyway.
    """
    from tools.network.ledger.scopes import set_covers

    candidates = []
    for hint in hints:
        if not hint.get("relay_url"):
            continue
        scopes = live_keys.get(hint["node"])
        if scopes and set_covers(scopes, "relay:serve"):
            candidates.append({"node": hint["node"], "relay_url": hint["relay_url"]})
    return candidates


def fetch_hints(registry_url: str, org: str, key, cert=None, *,
                node: Optional[str] = None, ts: Optional[int] = None) -> list:
    """Query the registry's reachability hints (Tier B: signed envelope).

    Imported lazily: the registry client bits (httpx + envelope signing)
    are only needed by callers that discover through the registry;
    callers may equally pass hints they already hold to ``dial_peer``.
    """
    import time as _time

    import httpx

    from tools.network.registry.signing import sign_request

    path = f"/v1/orgs/{org}/reachability/query"
    payload = {} if node is None else {"node": node}
    envelope = sign_request(key, "POST", path, payload,
                            ts=int(_time.time()) if ts is None else ts, cert=cert)
    response = httpx.post(f"{registry_url.rstrip('/')}{path}", json=envelope,
                          timeout=10.0)
    response.raise_for_status()
    return response.json()["hints"]


async def dial_via_peer_relay(
    relay_url: str,
    *,
    org: str,
    root_pub: str,
    target_pub: str,
    session: str,
    now: Optional[int] = None,
    timeout: float = 5.0,
) -> ViewerChannel:
    """One peer-relay attempt: verify the relay, then handshake through it.

    Raises :class:`~.peer.RelayVerifyError` when the relay cannot prove
    ``relay:serve`` — the refusal is ours, before any channel byte.
    """
    ws = await websockets.connect(
        f"{relay_url.rstrip('/')}/dial/{org}", max_size=2**22,
        compression=None, open_timeout=timeout,
    )
    try:
        nonce = secrets.token_hex(NONCE_HEX_LEN // 2)
        await ws.send(json.dumps({
            "v": RELAY_HELLO_VERSION, "purpose": "dial", "nonce": nonce,
            "target": target_pub, "session": session,
        }))
        verify_relay_hello(
            await ws.recv(), root_pub=root_pub, org=org, purpose="dial",
            nonce=nonce, target=target_pub, session=session, now=now,
        )

        ready = await ws.recv()  # {"ok": true} or close 4404 (not parked)
        if isinstance(ready, (bytes, bytearray)) or not json.loads(ready).get("ok"):
            raise ConnectionError("relay did not open a bridge")

        eph_priv, client_hello = build_client_hello()
        client_eph = eph_priv.public_key().public_bytes_raw().hex()
        await ws.send(client_hello)
        server_hello = await ws.recv()
        if isinstance(server_hello, str):
            raise HandshakeError("expected binary SERVER_HELLO")
        # Direct and peer-relay dials get the same tagged bytes as the
        # relayed path: serve_channel is transport-agnostic.
        server_hello = read_viewer_record(server_hello)
        server_eph, transcript_hash = verify_server_hello(
            server_hello, root_pub=root_pub, org=org, token=session,
            client_eph=client_eph, now=now,
        )
        return ViewerChannel(ws, ChannelCrypto.client(eph_priv, server_eph,
                                                      transcript_hash))
    except BaseException:
        with contextlib.suppress(Exception):
            await ws.close()
        raise


async def dial_peer(
    *,
    org: str,
    root_pub: str,
    target_pub: str,
    direct_addrs: Optional[list] = None,
    relays: Optional[list] = None,
    floor: Optional[tuple] = None,
    session: Optional[str] = None,
    attempt_timeout: float = 3.0,
    now: Optional[int] = None,
) -> DialResult:
    """Run the whole fallback chain to *target_pub*; first rung wins.

    *direct_addrs* — the target's candidate addresses (its hint row).
    *relays* — peer-relay candidates ``[{"node", "relay_url"}, ...]``
    (from :func:`relay_candidates`). *floor* — ``(relay_ws_url, token)``
    for the central B2 viewer channel. Rungs whose inputs are absent are
    skipped. Raises :class:`DialError` when everything failed.

    A failed relay *verification* (:class:`~.peer.RelayVerifyError`)
    falls through to the next candidate like any dead address — refusing
    an unauthorized relay must never strand the dial, only route around
    it; the refusal itself is recorded in ``attempts``.
    """
    session = session or new_session_id()
    attempts: list = []

    for addr in direct_addrs or []:
        try:
            channel = await direct_connect(
                addr, org=org, root_pub=root_pub, session=session,
                now=now, timeout=attempt_timeout,
            )
            return DialResult(channel, PATH_DIRECT, addr, attempts)
        except HandshakeError:
            raise  # a failed pin is an attack indicator, never "try elsewhere"
        except Exception as exc:
            attempts.append((PATH_DIRECT, addr, repr(exc)))

    for relay in relays or []:
        url = relay["relay_url"] if isinstance(relay, dict) else relay
        try:
            channel = await dial_via_peer_relay(
                url, org=org, root_pub=root_pub, target_pub=target_pub,
                session=session, now=now, timeout=attempt_timeout,
            )
            return DialResult(channel, PATH_PEER_RELAY, url, attempts)
        except HandshakeError:
            raise
        except Exception as exc:  # RelayVerifyError included: route around it
            attempts.append((PATH_PEER_RELAY, url, repr(exc)))

    if floor is not None:
        relay_url, token = floor
        try:
            channel = await ViewerChannel.connect(
                relay_url, token, root_pub=root_pub, org=org, now=now,
                open_timeout=attempt_timeout,
            )
            return DialResult(channel, PATH_FLOOR, relay_url, attempts)
        except HandshakeError:
            raise
        except Exception as exc:
            attempts.append((PATH_FLOOR, relay_url, repr(exc)))

    raise DialError(attempts)
