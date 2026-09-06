"""Mutually authenticated fleet channels over RelayKit's direct transport.

RelayKit's ordinary channel authenticates an organization-serving endpoint and
leaves the viewer anonymous. Personal-database synchronization has a different
trust boundary: both endpoints are machines in one personal-root-signed fleet.
This module keeps RelayKit's WebSocket transport, X25519/HKDF key schedule,
AEAD record layer, chunking, and request/response loop, while replacing only
the hello authentication.

The personal root public key is the external pin. Each hello proves possession
of a machine signing key that the receiver's current resolved roster marks
active. A roster entry alone is authorization, never authentication.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import time
from collections.abc import Callable, Iterable
from typing import Any

import websockets
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.network.fleet_roster import RosterEntry, resolve
from tools.network.clock import FLEET_RUNTIME_DELEGATION_TTL_SECONDS
from tools.network.idkit import (
    DelegationCert,
    IdkitError,
    KeyPair,
    canonical_json,
    verify_chain,
)
from tools.network.idkit.keys import verify_signature
from tools.network.relaykit.channel import ChannelCrypto, HandshakeError
from tools.network.relaykit.connector import serve_established_channel
from tools.network.relaykit.direct import DirectChannelServer, DIRECT_VERSION
from tools.network.relaykit.frames import VIEWER_KIND_RECORD, tag_viewer_message
from tools.network.relaykit.viewer import ViewerChannel, read_viewer_record

FLEET_HANDSHAKE_VERSION = 2
FLEET_HANDSHAKE_DOMAIN = b"autonomy.network.fleet-channel.handshake.v1\n"

#: Upper bound on how long a kicked machine can keep an already-open stream
#: alive: authorize() re-derives the roster at most this often per stream.
AUTHORIZE_CACHE_TTL_S = 1.0

_CLIENT_FIELDS = frozenset(
    {"v", "machine_pub", "eph_pub", "delegate_cert", "sig"}
)
_SERVER_FIELDS = frozenset(
    {
        "v", "machine_pub", "eph_pub", "client_machine_pub",
        "delegate_cert", "sig",
    }
)


def _hex64(value: object, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise HandshakeError(f"{what} must be 64 lowercase hex chars")
    return value


def _parse(raw: object, fields: frozenset[str], what: str) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HandshakeError(f"{what} is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HandshakeError(f"{what} is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != fields:
        raise HandshakeError(f"{what} must carry exactly {sorted(fields)}")
    if data["v"] != FLEET_HANDSHAKE_VERSION:
        raise HandshakeError(f"unsupported {what} version: {data['v']!r}")
    _hex64(data["machine_pub"], f"{what} machine_pub")
    _hex64(data["eph_pub"], f"{what} eph_pub")
    if not isinstance(data["sig"], str):
        raise HandshakeError(f"{what} sig must be a string")
    if data["delegate_cert"] is not None and not isinstance(
        data["delegate_cert"], dict
    ):
        raise HandshakeError(f"{what} delegate_cert must be an object or null")
    return data


def _eph_pub(private_key: X25519PrivateKey) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def _client_payload(
    *, root_pub: str, session: str, machine_pub: str, eph_pub: str,
    delegate_cert,
) -> bytes:
    return FLEET_HANDSHAKE_DOMAIN + canonical_json(
        {
            "v": FLEET_HANDSHAKE_VERSION,
            "side": "client",
            "root_pub": root_pub,
            "session": session,
            "machine_pub": machine_pub,
            "eph_pub": eph_pub,
            "delegate_cert": delegate_cert,
        }
    )


def _server_payload(
    *, root_pub: str, session: str, client_machine_pub: str,
    client_eph: str, machine_pub: str, eph_pub: str, delegate_cert,
) -> bytes:
    return FLEET_HANDSHAKE_DOMAIN + canonical_json(
        {
            "v": FLEET_HANDSHAKE_VERSION,
            "side": "server",
            "root_pub": root_pub,
            "session": session,
            "client_machine_pub": client_machine_pub,
            "client_eph": client_eph,
            "machine_pub": machine_pub,
            "eph_pub": eph_pub,
            "delegate_cert": delegate_cert,
        }
    )


def _transcript_hash(
    *, root_pub: str, session: str, client_machine_pub: str,
    client_eph: str, server_machine_pub: str, server_eph: str,
) -> bytes:
    return hashlib.sha256(
        FLEET_HANDSHAKE_DOMAIN
        + canonical_json(
            {
                "v": FLEET_HANDSHAKE_VERSION,
                "root_pub": root_pub,
                "session": session,
                "client_machine_pub": client_machine_pub,
                "client_eph": client_eph,
                "server_machine_pub": server_machine_pub,
                "server_eph": server_eph,
            }
        )
    ).digest()


class FleetAuthenticator:
    """Machine-key possession and live-roster authorization for one endpoint."""

    def __init__(
        self,
        machine_key: KeyPair,
        *,
        root_pub: str,
        roster_entries: Callable[[], Iterable[RosterEntry]],
        roster_machine_pub: str | None = None,
        delegation_cert: DelegationCert | None = None,
        require_delegation: bool = False,
    ):
        # ``machine_key`` is the live signer. In production it is a
        # process-ephemeral child; ``roster_machine_pub`` is the durable,
        # root-authorized identity. Tests may deliberately omit the
        # delegation to exercise the lower transport without browser custody.
        self.machine_key = machine_key
        self.machine_pub = _hex64(
            roster_machine_pub or machine_key.public_hex,
            "fleet roster machine_pub",
        )
        self.delegation_cert = delegation_cert
        self.require_delegation = bool(require_delegation)
        self.root_pub = _hex64(root_pub, "fleet root_pub")
        self._roster_entries = roster_entries
        #: (monotonic, active machine_pubs) — see authorize().
        self._active_cache: tuple[float, frozenset[str]] | None = None

    def invalidate_authorization_cache(self) -> None:
        """Drop the cached roster so the next authorize() re-derives it.
        Callers that OWN the roster change point (the scheduler's snapshot
        refresh) call this to make a kick land immediately rather than
        within AUTHORIZE_CACHE_TTL_S."""
        self._active_cache = None

    def authorize(self, machine_pub: str) -> None:
        # Called per served transaction AND per operation so a kick lands on
        # an already-open stream. The resolved roster is a few hundred bytes
        # and changes only on a human enroll/kick, yet re-deriving it means a
        # roster read plus an ed25519 verify per entry (~0.5ms) — which a
        # first-contact journal replay multiplied 1.4 million times into ~12
        # minutes of CPU per pull (live 2026-09-06). Cache it for a bounded
        # window: a kick still takes effect within AUTHORIZE_CACHE_TTL_S.
        now = time.monotonic()
        cached = self._active_cache
        if cached is None or now - cached[0] > AUTHORIZE_CACHE_TTL_S:
            active = frozenset(resolve(
                self._roster_entries(), anchor_root_pub=self.root_pub
            ))
            self._active_cache = (now, active)
        else:
            active = cached[1]
        if machine_pub not in active:
            raise HandshakeError("machine key is not active in this fleet roster")

    def _delegate_dict(self):
        return (
            self.delegation_cert.to_dict()
            if self.delegation_cert is not None
            else None
        )

    def _authorize_local_signer(self, delegate) -> None:
        if delegate is None:
            self.authorize(self.machine_pub)
            if self.require_delegation:
                raise HandshakeError("fleet runtime delegation is required")
            if self.machine_key.public_hex != self.machine_pub:
                raise HandshakeError("direct fleet signer does not match roster")
            return
        signer_pub = self._signing_pub(self.machine_pub, delegate)
        if signer_pub != self.machine_key.public_hex:
            raise HandshakeError(
                "fleet runtime key does not match its delegation"
            )

    def _signing_pub(self, machine_pub: str, delegate_data) -> str:
        """Resolve one hello signer from current root-signed roster state."""
        self.authorize(machine_pub)
        if delegate_data is None:
            if self.require_delegation:
                raise HandshakeError("fleet runtime delegation is required")
            return machine_pub
        try:
            cert = DelegationCert.from_dict(delegate_data)
            active = resolve(
                self._roster_entries(), anchor_root_pub=self.root_pub
            )
            entry = active[machine_pub]
            expected_org = f"personal:{self.root_pub}"
            verified = verify_chain(
                cert,
                machine_pub,
                org=expected_org,
                now=int(time.time()),
                required_scope="fleet:sync",
            )
            if cert.parent_cert is not None:
                raise HandshakeError(
                    "fleet runtime delegation must be machine-direct"
                )
            if cert.scope != ("fleet:sync",):
                raise HandshakeError(
                    "fleet runtime delegation has excess scope"
                )
            if (
                cert.not_after - cert.not_before
                > FLEET_RUNTIME_DELEGATION_TTL_SECONDS + 60
            ):
                raise HandshakeError(
                    "fleet runtime delegation exceeds its TTL bound"
                )
            if cert.target_types is not None:
                raise HandshakeError(
                    "fleet runtime delegation must not carry target_types"
                )
            if (
                verified.subject_kind != "machine"
                or verified.subject_id != entry.machine_id
            ):
                raise HandshakeError(
                    "fleet runtime delegation names another machine"
                )
            return verified.leaf_pub
        except HandshakeError:
            raise
        except (IdkitError, KeyError, ValueError, TypeError) as exc:
            raise HandshakeError(
                f"fleet runtime delegation failed: {exc}"
            ) from exc

    def build_client_hello(self, session: str) -> tuple[X25519PrivateKey, bytes]:
        delegate = self._delegate_dict()
        self._authorize_local_signer(delegate)
        private_key = X25519PrivateKey.generate()
        eph_pub = _eph_pub(private_key)
        body = {
            "v": FLEET_HANDSHAKE_VERSION,
            "machine_pub": self.machine_pub,
            "eph_pub": eph_pub,
            "delegate_cert": delegate,
        }
        body["sig"] = self.machine_key.sign_hex(
            _client_payload(
                root_pub=self.root_pub,
                session=session,
                machine_pub=self.machine_pub,
                eph_pub=eph_pub,
                delegate_cert=delegate,
            )
        )
        return private_key, canonical_json(body)

    def accept_client(
        self, raw: object, *, session: str
    ) -> tuple[str, X25519PrivateKey, bytes, bytes]:
        data = _parse(raw, _CLIENT_FIELDS, "FLEET_CLIENT_HELLO")
        client_pub = data["machine_pub"]
        self._authorize_local_signer(self._delegate_dict())
        signer_pub = self._signing_pub(client_pub, data["delegate_cert"])
        try:
            verify_signature(
                signer_pub,
                data["sig"],
                _client_payload(
                    root_pub=self.root_pub,
                    session=session,
                    machine_pub=client_pub,
                    eph_pub=data["eph_pub"],
                    delegate_cert=data["delegate_cert"],
                ),
            )
        except IdkitError as exc:
            raise HandshakeError(f"client machine proof failed: {exc}") from exc

        private_key = X25519PrivateKey.generate()
        server_eph = _eph_pub(private_key)
        body = {
            "v": FLEET_HANDSHAKE_VERSION,
            "machine_pub": self.machine_pub,
            "eph_pub": server_eph,
            "client_machine_pub": client_pub,
            "delegate_cert": self._delegate_dict(),
        }
        body["sig"] = self.machine_key.sign_hex(
            _server_payload(
                root_pub=self.root_pub,
                session=session,
                client_machine_pub=client_pub,
                client_eph=data["eph_pub"],
                machine_pub=self.machine_pub,
                eph_pub=server_eph,
                delegate_cert=self._delegate_dict(),
            )
        )
        transcript = _transcript_hash(
            root_pub=self.root_pub,
            session=session,
            client_machine_pub=client_pub,
            client_eph=data["eph_pub"],
            server_machine_pub=self.machine_pub,
            server_eph=server_eph,
        )
        return client_pub, private_key, canonical_json(body), transcript

    def verify_server(
        self,
        raw: object,
        *,
        session: str,
        client_eph: str,
        expected_machine_pub: str,
    ) -> tuple[str, bytes]:
        data = _parse(raw, _SERVER_FIELDS, "FLEET_SERVER_HELLO")
        if data["client_machine_pub"] != self.machine_pub:
            raise HandshakeError("server hello names another client machine")
        if data["machine_pub"] != expected_machine_pub:
            raise HandshakeError("server hello is from an unexpected machine")
        signer_pub = self._signing_pub(
            data["machine_pub"], data["delegate_cert"]
        )
        try:
            verify_signature(
                signer_pub,
                data["sig"],
                _server_payload(
                    root_pub=self.root_pub,
                    session=session,
                    client_machine_pub=self.machine_pub,
                    client_eph=client_eph,
                    machine_pub=data["machine_pub"],
                    eph_pub=data["eph_pub"],
                    delegate_cert=data["delegate_cert"],
                ),
            )
        except IdkitError as exc:
            raise HandshakeError(f"server machine proof failed: {exc}") from exc
        return data["eph_pub"], _transcript_hash(
            root_pub=self.root_pub,
            session=session,
            client_machine_pub=self.machine_pub,
            client_eph=client_eph,
            server_machine_pub=data["machine_pub"],
            server_eph=data["eph_pub"],
        )


class FleetDirectServer:
    """A RelayKit direct listener authenticated by the personal fleet roster."""

    def __init__(
        self,
        authenticator: FleetAuthenticator,
        handler,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.authenticator = authenticator
        self._server = DirectChannelServer(
            None,
            None,
            None,
            handler,
            host=host,
            port=port,
            channel_server=self._serve,
        )

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def connection_count(self) -> int:
        return self._server.connection_count

    @property
    def running(self) -> bool:
        return self._server.running

    @property
    def host(self) -> str:
        return self._server._host

    async def start(self) -> int:
        return await self._server.start()

    async def stop(self) -> None:
        await self._server.stop()

    async def _serve(self, *, token: str, recv, send, handler) -> None:
        raw = await recv()
        if raw is None:
            return
        client_pub, private_key, hello, transcript = (
            self.authenticator.accept_client(raw, session=token)
        )
        await send(tag_viewer_message(VIEWER_KIND_RECORD, hello))
        crypto = ChannelCrypto.server(
            private_key,
            _parse(raw, _CLIENT_FIELDS, "FLEET_CLIENT_HELLO")["eph_pub"],
            transcript,
        )

        async def authorized_handler(channel_token: str, message: bytes):
            # Re-resolve on every application message. A kick takes effect on
            # an already-open socket before any further data is accepted.
            self.authenticator.authorize(client_pub)
            response = handler(channel_token, message, client_pub)
            if inspect.isawaitable(response):
                response = await response
            return response

        await serve_established_channel(
            crypto,
            token=token,
            recv=recv,
            send=send,
            handler=authorized_handler,
        )


async def fleet_direct_connect(
    addr: str,
    *,
    authenticator: FleetAuthenticator,
    expected_machine_pub: str,
    session: str,
    timeout: float = 3.0,
) -> ViewerChannel:
    """Open one mutually authenticated fleet channel over a direct address."""

    async def attempt() -> ViewerChannel:
        # ping/pong pinned, not defaulted: the sync stream liveness policy
        # (auto-fzy8s) counts on this layer to break the socket for dead
        # and frozen peers — recv unblocks in ping_interval + ping_timeout
        # + close_timeout (measured 50.0s) — leaving only wedged-but-
        # responsive serves to the application-level silence bounds.
        ws = await websockets.connect(
            addr, max_size=2**22, compression=None, open_timeout=timeout,
            ping_interval=20, ping_timeout=20,
        )
        try:
            await ws.send(json.dumps({"v": DIRECT_VERSION, "session": session}))
            private_key, hello = authenticator.build_client_hello(session)
            client_eph = _parse(
                hello, _CLIENT_FIELDS, "FLEET_CLIENT_HELLO"
            )["eph_pub"]
            await ws.send(hello)
            server_hello = await ws.recv()
            if isinstance(server_hello, str):
                raise HandshakeError("expected binary FLEET_SERVER_HELLO")
            server_eph, transcript = authenticator.verify_server(
                read_viewer_record(server_hello),
                session=session,
                client_eph=client_eph,
                expected_machine_pub=expected_machine_pub,
            )
            return ViewerChannel(
                ws, ChannelCrypto.client(private_key, server_eph, transcript)
            )
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise

    return await asyncio.wait_for(attempt(), timeout=timeout)
