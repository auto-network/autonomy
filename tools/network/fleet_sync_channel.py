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
from collections.abc import Callable, Iterable
from typing import Any

import websockets
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.network.fleet_roster import RosterEntry, resolve
from tools.network.idkit import IdkitError, KeyPair, canonical_json
from tools.network.idkit.keys import verify_signature
from tools.network.relaykit.channel import ChannelCrypto, HandshakeError
from tools.network.relaykit.connector import serve_established_channel
from tools.network.relaykit.direct import DirectChannelServer, DIRECT_VERSION
from tools.network.relaykit.frames import VIEWER_KIND_RECORD, tag_viewer_message
from tools.network.relaykit.viewer import ViewerChannel, read_viewer_record

FLEET_HANDSHAKE_VERSION = 1
FLEET_HANDSHAKE_DOMAIN = b"autonomy.network.fleet-channel.handshake.v1\n"

_CLIENT_FIELDS = frozenset({"v", "machine_pub", "eph_pub", "sig"})
_SERVER_FIELDS = frozenset(
    {"v", "machine_pub", "eph_pub", "client_machine_pub", "sig"}
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
    return data


def _eph_pub(private_key: X25519PrivateKey) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def _client_payload(
    *, root_pub: str, session: str, machine_pub: str, eph_pub: str
) -> bytes:
    return FLEET_HANDSHAKE_DOMAIN + canonical_json(
        {
            "v": FLEET_HANDSHAKE_VERSION,
            "side": "client",
            "root_pub": root_pub,
            "session": session,
            "machine_pub": machine_pub,
            "eph_pub": eph_pub,
        }
    )


def _server_payload(
    *, root_pub: str, session: str, client_machine_pub: str,
    client_eph: str, machine_pub: str, eph_pub: str,
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
    ):
        self.machine_key = machine_key
        self.root_pub = _hex64(root_pub, "fleet root_pub")
        self._roster_entries = roster_entries

    def authorize(self, machine_pub: str) -> None:
        active = resolve(
            self._roster_entries(), anchor_root_pub=self.root_pub
        )
        if machine_pub not in active:
            raise HandshakeError("machine key is not active in this fleet roster")

    def build_client_hello(self, session: str) -> tuple[X25519PrivateKey, bytes]:
        self.authorize(self.machine_key.public_hex)
        private_key = X25519PrivateKey.generate()
        eph_pub = _eph_pub(private_key)
        body = {
            "v": FLEET_HANDSHAKE_VERSION,
            "machine_pub": self.machine_key.public_hex,
            "eph_pub": eph_pub,
        }
        body["sig"] = self.machine_key.sign_hex(
            _client_payload(
                root_pub=self.root_pub,
                session=session,
                machine_pub=self.machine_key.public_hex,
                eph_pub=eph_pub,
            )
        )
        return private_key, canonical_json(body)

    def accept_client(
        self, raw: object, *, session: str
    ) -> tuple[str, X25519PrivateKey, bytes, bytes]:
        data = _parse(raw, _CLIENT_FIELDS, "FLEET_CLIENT_HELLO")
        client_pub = data["machine_pub"]
        self.authorize(self.machine_key.public_hex)
        self.authorize(client_pub)
        try:
            verify_signature(
                client_pub,
                data["sig"],
                _client_payload(
                    root_pub=self.root_pub,
                    session=session,
                    machine_pub=client_pub,
                    eph_pub=data["eph_pub"],
                ),
            )
        except IdkitError as exc:
            raise HandshakeError(f"client machine proof failed: {exc}") from exc

        private_key = X25519PrivateKey.generate()
        server_eph = _eph_pub(private_key)
        body = {
            "v": FLEET_HANDSHAKE_VERSION,
            "machine_pub": self.machine_key.public_hex,
            "eph_pub": server_eph,
            "client_machine_pub": client_pub,
        }
        body["sig"] = self.machine_key.sign_hex(
            _server_payload(
                root_pub=self.root_pub,
                session=session,
                client_machine_pub=client_pub,
                client_eph=data["eph_pub"],
                machine_pub=self.machine_key.public_hex,
                eph_pub=server_eph,
            )
        )
        transcript = _transcript_hash(
            root_pub=self.root_pub,
            session=session,
            client_machine_pub=client_pub,
            client_eph=data["eph_pub"],
            server_machine_pub=self.machine_key.public_hex,
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
        if data["client_machine_pub"] != self.machine_key.public_hex:
            raise HandshakeError("server hello names another client machine")
        if data["machine_pub"] != expected_machine_pub:
            raise HandshakeError("server hello is from an unexpected machine")
        self.authorize(data["machine_pub"])
        try:
            verify_signature(
                data["machine_pub"],
                data["sig"],
                _server_payload(
                    root_pub=self.root_pub,
                    session=session,
                    client_machine_pub=self.machine_key.public_hex,
                    client_eph=client_eph,
                    machine_pub=data["machine_pub"],
                    eph_pub=data["eph_pub"],
                ),
            )
        except IdkitError as exc:
            raise HandshakeError(f"server machine proof failed: {exc}") from exc
        return data["eph_pub"], _transcript_hash(
            root_pub=self.root_pub,
            session=session,
            client_machine_pub=self.machine_key.public_hex,
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
        ws = await websockets.connect(
            addr, max_size=2**22, compression=None, open_timeout=timeout
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
