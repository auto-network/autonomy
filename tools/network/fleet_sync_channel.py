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
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import websockets
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.network.fleet_roster import RosterEntry, resolve
from tools.network.clock import FLEET_RUNTIME_DELEGATION_TTL_SECONDS
from tools.network.fleet_process_scope import (
    FLEET_SYNC_SCOPE,
    SCOPE_EXCESS,
    scope_problem,
)
from tools.network.idkit import (
    DelegationCert,
    IdkitError,
    KeyPair,
    canonical_json,
    verify_chain,
)
from tools.network.idkit.errors import (
    ExpiredError,
    MalformedError,
    NotYetValidError,
    RevokedError,
    ScopeError,
    SignatureError,
    WrongOrgError,
)
from tools.network.idkit.keys import verify_signature
from tools.network.relaykit.channel import ChannelCrypto, HandshakeError
from tools.network.relaykit.connector import serve_established_channel
from tools.network.relaykit.direct import DirectChannelServer, DIRECT_VERSION
from tools.network.relaykit.frames import VIEWER_KIND_RECORD, tag_viewer_message
from tools.network.relaykit.viewer import ViewerChannel, read_viewer_record

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.network.fleet_org_channel import OrgFleetAuthenticator

FLEET_HANDSHAKE_VERSION = 2
from tools.network import clock

FLEET_HANDSHAKE_DOMAIN = b"autonomy.network.fleet-channel.handshake.v1\n"


_CLIENT_FIELDS = frozenset(
    {"v", "machine_pub", "eph_pub", "delegate_cert", "sig"}
)
_SERVER_FIELDS = frozenset(
    {
        "v", "machine_pub", "eph_pub", "client_machine_pub",
        "delegate_cert", "sig",
    }
)


logger = logging.getLogger(__name__)

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


class FleetHandshakeRefused(HandshakeError):
    """A handshake check that failed, named by a typed ``refusal`` code.

    Codes are stated from the machine RUNNING the check: ``own-*`` is about
    its own runtime material (roster entry, process delegation), ``peer-*``
    about the hello the other machine sent. Every raise site has its own
    code, so a refusal says which check failed and on whose material; the
    message keeps the human detail. Being a HandshakeError, it fails the
    handshake exactly as before for every existing caller.
    """

    def __init__(self, refusal: str, detail: str):
        super().__init__(f"{refusal}: {detail}")
        self.refusal = refusal
        self.detail = detail


#: Every code FleetHandshakeRefused can carry. A refusal a peer reports
#: before the handshake is unauthenticated, so a receiver accepts only these
#: (plus its own protocol's) and maps anything else to one generic code.
_SIDED_REFUSALS = (
    "not-in-roster", "scope-undelegated", "scope-missing", "scope-excess",
    "delegation-required", "delegation-expired", "delegation-not-yet-valid",
    "delegation-revoked", "delegation-bad-signature", "delegation-wrong-org",
    "delegation-malformed", "delegation-invalid", "delegation-not-machine-direct",
    "delegation-ttl-exceeded", "delegation-target-types",
    "delegation-wrong-machine",
)
HANDSHAKE_REFUSALS = frozenset(
    [f"{side}-{suffix}" for side in ("own", "peer") for suffix in _SIDED_REFUSALS]
    + ["own-signer-not-roster-key", "own-key-not-delegated",
       "peer-hello-malformed", "peer-client-proof-failed",
       "peer-server-proof-failed", "peer-server-wrong-client",
       "peer-server-unexpected-machine"]
)

#: idkit chain failures, by type, as the ``<side>-delegation-*`` suffix.
_CHAIN_REFUSALS = (
    (ExpiredError, "expired"),
    (NotYetValidError, "not-yet-valid"),
    (RevokedError, "revoked"),
    (SignatureError, "bad-signature"),
    (WrongOrgError, "wrong-org"),
    (MalformedError, "malformed"),
)


def _chain_refusal(side: str, exc: Exception) -> FleetHandshakeRefused:
    for kind, suffix in _CHAIN_REFUSALS:
        if isinstance(exc, kind):
            return FleetHandshakeRefused(
                f"{side}-delegation-{suffix}", f"fleet runtime delegation failed: {exc}")
    return FleetHandshakeRefused(
        f"{side}-delegation-invalid", f"fleet runtime delegation failed: {exc}")


class FleetAuthenticator:
    """Machine-key possession and live-roster authorization for one endpoint.

    ``scope`` is the scope every process delegation this endpoint sends or
    accepts must INCLUDE: ``fleet:sync`` for sync, ``session:control`` for
    remote session control. Both ride one delegation
    (fleet_process_scope); a delegation carrying anything outside that set
    is refused, as is one without the scope this endpoint verifies.
    """

    def __init__(
        self,
        machine_key: KeyPair,
        *,
        root_pub: str,
        roster_entries: Callable[[], Iterable[RosterEntry]],
        roster_machine_pub: str | None = None,
        delegation_cert: DelegationCert | None = None,
        require_delegation: bool = False,
        scope: str = FLEET_SYNC_SCOPE,
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
        self.scope = scope
        self.root_pub = _hex64(root_pub, "fleet root_pub")
        self._roster_entries = roster_entries
        #: (monotonic, active machine_pubs) — see authorize().
        self._active_cache: tuple[float, frozenset[str]] | None = None

    def invalidate_authorization_cache(self) -> None:
        """Drop the cached roster so the next authorize() re-derives it.
        Callers that OWN the roster change point (the scheduler's snapshot
        refresh) call this to make a kick land immediately rather than
        within clock.AUTHORIZE_CACHE_TTL_S."""
        self._active_cache = None

    def authorize(self, machine_pub: str, *, side: str = "peer") -> None:
        # Called per served transaction AND per operation so a kick lands on
        # an already-open stream. The resolved roster is a few hundred bytes
        # and changes only on a human enroll/kick, yet re-deriving it means a
        # roster read plus an ed25519 verify per entry (~0.5ms) — which a
        # first-contact journal replay multiplied 1.4 million times into ~12
        # minutes of CPU per pull (live 2026-09-06). Cache it for a bounded
        # window: a kick still takes effect within clock.AUTHORIZE_CACHE_TTL_S.
        now = time.monotonic()
        cached = self._active_cache
        if cached is None or now - cached[0] > clock.AUTHORIZE_CACHE_TTL_S:
            active = frozenset(resolve(
                self._roster_entries(), anchor_root_pub=self.root_pub
            ))
            self._active_cache = (now, active)
        else:
            active = cached[1]
        if machine_pub not in active:
            raise FleetHandshakeRefused(
                f"{side}-not-in-roster",
                "machine key is not active in this fleet roster")

    def _delegate_dict(self):
        return (
            self.delegation_cert.to_dict()
            if self.delegation_cert is not None
            else None
        )

    def _authorize_local_signer(self, delegate) -> None:
        if delegate is None:
            self.authorize(self.machine_pub, side="own")
            if self.scope != FLEET_SYNC_SCOPE:
                raise FleetHandshakeRefused(
                    "own-scope-undelegated",
                    f"this runtime holds no process delegation, so no {self.scope}")
            if self.require_delegation:
                raise FleetHandshakeRefused(
                    "own-delegation-required", "fleet runtime delegation is required")
            if self.machine_key.public_hex != self.machine_pub:
                raise FleetHandshakeRefused(
                    "own-signer-not-roster-key", "direct fleet signer does not match roster")
            return
        signer_pub = self._signing_pub(self.machine_pub, delegate, side="own")
        if signer_pub != self.machine_key.public_hex:
            raise FleetHandshakeRefused(
                "own-key-not-delegated",
                "fleet runtime key does not match its delegation")

    def _signing_pub(self, machine_pub: str, delegate_data, *, side: str = "peer") -> str:
        """Resolve one hello signer from current root-signed roster state.

        *side* says whose material this is (``own`` for this endpoint's
        delegation, ``peer`` for the other machine's hello) and prefixes
        every refusal code."""
        self.authorize(machine_pub, side=side)
        if delegate_data is None:
            if self.scope != FLEET_SYNC_SCOPE:
                raise FleetHandshakeRefused(
                    f"{side}-scope-undelegated",
                    f"hello carries no process delegation, so no {self.scope}")
            if self.require_delegation:
                raise FleetHandshakeRefused(
                    f"{side}-delegation-required", "fleet runtime delegation is required")
            return machine_pub
        try:
            cert = DelegationCert.from_dict(delegate_data)
        except (IdkitError, KeyError, ValueError, TypeError) as exc:
            raise _chain_refusal(side, exc) from exc
        try:
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
                required_scope=self.scope,
            )
        except ScopeError as exc:
            raise FleetHandshakeRefused(
                f"{side}-scope-missing",
                f"delegation scope {list(cert.scope)} does not include {self.scope!r}",
            ) from exc
        except (IdkitError, KeyError, ValueError, TypeError) as exc:
            raise _chain_refusal(side, exc) from exc
        if scope_problem(cert.scope, self.scope) == SCOPE_EXCESS:
            raise FleetHandshakeRefused(
                f"{side}-scope-excess",
                f"delegation scope {list(cert.scope)} carries authority outside "
                "the process scopes")
        if cert.parent_cert is not None:
            raise FleetHandshakeRefused(
                f"{side}-delegation-not-machine-direct",
                "fleet runtime delegation must be machine-direct")
        if (
            cert.not_after - cert.not_before
            > FLEET_RUNTIME_DELEGATION_TTL_SECONDS + 60
        ):
            raise FleetHandshakeRefused(
                f"{side}-delegation-ttl-exceeded",
                "fleet runtime delegation exceeds its TTL bound")
        if cert.target_types is not None:
            raise FleetHandshakeRefused(
                f"{side}-delegation-target-types",
                "fleet runtime delegation must not carry target_types")
        if (
            verified.subject_kind != "machine"
            or verified.subject_id != entry.machine_id
        ):
            raise FleetHandshakeRefused(
                f"{side}-delegation-wrong-machine",
                "fleet runtime delegation names another machine")
        return verified.leaf_pub

    def build_client_hello(
        self, session: str, *, peer: str | None = None,
    ) -> tuple[X25519PrivateKey, bytes]:
        """*peer* (the machine being dialled) is the org authenticator's
        concern; the personal hello is the same for every fleet machine."""
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
        try:
            data = _parse(raw, _CLIENT_FIELDS, "FLEET_CLIENT_HELLO")
        except HandshakeError as exc:
            raise FleetHandshakeRefused("peer-hello-malformed", str(exc)) from exc
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
            raise FleetHandshakeRefused(
                "peer-client-proof-failed", f"client machine proof failed: {exc}") from exc

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
        try:
            data = _parse(raw, _SERVER_FIELDS, "FLEET_SERVER_HELLO")
        except HandshakeError as exc:
            raise FleetHandshakeRefused("peer-hello-malformed", str(exc)) from exc
        if data["client_machine_pub"] != self.machine_pub:
            raise FleetHandshakeRefused(
                "peer-server-wrong-client", "server hello names another client machine")
        if data["machine_pub"] != expected_machine_pub:
            # Name both keys: the bare message cannot distinguish "dialed the
            # wrong machine's route" from "this machine serves under a
            # different key than the roster entry names", and those have
            # opposite fixes.
            raise FleetHandshakeRefused(
                "peer-server-unexpected-machine",
                "server hello is from an unexpected machine: expected "
                f"{str(expected_machine_pub)[:16]}, got "
                f"{str(data['machine_pub'])[:16]}"
            )
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
            raise FleetHandshakeRefused(
                "peer-server-proof-failed", f"server machine proof failed: {exc}") from exc
        return data["eph_pub"], _transcript_hash(
            root_pub=self.root_pub,
            session=session,
            client_machine_pub=self.machine_pub,
            client_eph=client_eph,
            server_machine_pub=data["machine_pub"],
            server_eph=data["eph_pub"],
        )


@dataclass(frozen=True)
class Admission:
    """The outcome of accepting one client hello, whichever hello it was.

    ``org`` is None for a hello admitted by the PERSONAL roster and the
    organization's genesis id for one admitted by the org hello
    (fleet_org_channel); ``authorize`` is the per-message re-check of the
    authenticator that admitted the peer.

    ``kind`` names which authenticated path built the admission — ``fleet``
    for a personal-roster hello, ``org`` for an org hello, and ``follow`` for
    an admission the link server derives from an ``org:follow`` grant with no
    client credential at all (design of record graph://5f2f5a49-00d §10.1). A
    ``follow`` admission carries only ``kind`` and ``org``; it never went
    through a handshake, so its crypto fields are absent and its per-message
    authorizer is a no-op — authenticity is the link's fragment key, checked
    once at the viewer handshake.
    """

    client_pub: str = ""
    client_eph: str = ""
    private_key: "X25519PrivateKey | None" = None
    server_hello: bytes = b""
    transcript: bytes = b""
    authorize: "Callable[[str], None] | None" = None
    org: str | None = None
    kind: str = "fleet"


def hello_org(raw: object) -> str | None:
    """The ``org`` an incoming client hello names, or None for a personal
    hello. Only the shape is read here; every check is the authenticator's.
    The personal hello has no ``org`` field, so its bytes and its path are
    untouched by this dispatch."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HandshakeError("client hello is not valid UTF-8") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HandshakeError("client hello is not valid JSON") from exc
    if not isinstance(data, dict):
        raise HandshakeError("client hello must be an object")
    org = data.get("org")
    if org is None:
        return None
    if not isinstance(org, str) or not org:
        raise HandshakeError("client hello org must be a non-empty string")
    return org


def accept_client_hello(
    raw: object,
    *,
    session: str,
    authenticator: FleetAuthenticator,
    org_channel_for: "Callable[[str], OrgFleetAuthenticator | None] | None" = None,
) -> Admission:
    """Admit one client hello: the personal roster's for a personal hello,
    the org hello's authenticator for a hello naming an ``org``.

    A hello naming an organization this machine has no org channel for is
    refused with a typed error; nothing about that organization is learned
    from an unadmitted peer.
    """
    org = hello_org(raw)
    if org is None:
        client_pub, private_key, hello, transcript = (
            authenticator.accept_client(raw, session=session)
        )
        client_eph = _parse(raw, _CLIENT_FIELDS, "FLEET_CLIENT_HELLO")["eph_pub"]
        return Admission(
            client_pub, client_eph, private_key, hello, transcript,
            authenticator.authorize, None, kind="fleet",
        )
    channel = org_channel_for(org) if org_channel_for is not None else None
    if channel is None:
        raise HandshakeError(
            f"no organization scope on this machine admits org {org[:16]}..."
        )
    client_pub, private_key, hello, transcript = (
        channel.accept_client(raw, session=session)
    )
    client_eph = json.loads(raw)["eph_pub"]
    return Admission(
        client_pub, client_eph, private_key, hello, transcript,
        channel.authorize, channel.org, kind="org",
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
        org_channel_for: "Callable[[str], OrgFleetAuthenticator | None] | None" = None,
    ):
        """``org_channel_for(org)`` returns the org hello authenticator for
        an organization's genesis id, or None; without it every hello
        naming an org is refused and the listener is the personal one."""
        self.authenticator = authenticator
        self._org_channel_for = org_channel_for
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

    async def _serve(self, *, token: str, recv, send, handler, close=None, ping=None) -> None:
        await serve_fleet_transport(
            token=token, recv=recv, send=send, handler=handler, close=close,
            authenticator=self.authenticator,
            org_channel_for=self._org_channel_for, ping=ping,
        )


#: Direct-channel liveness (auto-fkqz6): while an endpoint WAITS ON ITS PEER
#: (the puller for the next frame, the listener for the next request) it
#: pings every DIRECT_PING_INTERVAL_S and declares the peer dead only when,
#: DIRECT_PING_TIMEOUT_S later, neither a frame nor the pong has arrived —
#: judged by done() on wake, never by a wall deadline, so a stall of the
#: local event loop (SJC-2's puller loop: 7-28 s, 2026-09-29) cannot fail a
#: peer that answered. No ping is in flight while an endpoint is busy with
#: its own work (applying a batch, building a page): the peer's silence is
#: then not the peer's fault. Dead peers are still caught in
#: interval + timeout = 40 s on any wait, as the 2026-09-03 policy wanted.
DIRECT_PING_INTERVAL_S = 20.0
DIRECT_PING_TIMEOUT_S = 20.0
#: A serve's send that cannot complete in this long means the peer stopped
#: reading: a puller reads continuously except while it applies one bounded
#: batch (APPLY_BATCH_TRANSACTIONS / APPLY_FLUSH_INTERVAL_S) or its loop
#: stalls; two minutes is an order of magnitude above both. Progress, not
#: pong latency, is the liveness signal of a streaming serve (the relay pull
#: learned the same on 2026-09-06: fleet_relay_sync.PULL_PING_TIMEOUT_S).
SERVE_SEND_STALL_S = 120.0


class PeerUnresponsive(ConnectionError):
    """A wait on the peer ended with neither data nor a pong: the peer is
    dead or frozen (killed, SIGSTOPped, its event loop starved)."""


async def wait_alive(awaitable, *, ping, interval_s: float | None = None,
                     timeout_s: float | None = None):
    """Await *awaitable* while proving the peer alive.

    Whenever nothing has arrived for ``interval_s``, send a ping; the peer is
    declared dead only if ``timeout_s`` later NEITHER the awaitable NOR the
    pong has completed. Both are checked with ``done()`` after the wait
    returns, so a local loop stall longer than the timeout — during which
    the pong arrived and sits in the socket — resolves as alive the moment
    the loop wakes. ``ping`` is the transport's ``ping()``; a ping that
    cannot even be SENT within ``timeout_s`` (the peer stopped draining our
    writes) is the same verdict. With ``ping=None`` this is a plain await."""
    if ping is None:
        return await awaitable
    # Read at call time, not bound at definition: the constants are the
    # policy, and a test shrinks them.
    interval_s = DIRECT_PING_INTERVAL_S if interval_s is None else interval_s
    timeout_s = DIRECT_PING_TIMEOUT_S if timeout_s is None else timeout_s
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=interval_s)
            if task in done:
                return task.result()
            try:
                pong = await asyncio.wait_for(ping(), timeout_s)
            except asyncio.TimeoutError:
                raise PeerUnresponsive(
                    f"peer stopped reading: a ping could not be sent within {timeout_s:g}s"
                ) from None
            done, _pending = await asyncio.wait(
                {task, pong}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            if pong.done() and not pong.cancelled() and pong.exception() is None:
                continue   # alive: keep waiting for the data
            raise PeerUnresponsive(
                f"peer sent neither data nor a pong within {timeout_s:g}s of a ping"
            )
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


#: After a reply has been fully written, the listener waits for the puller
#: to close (a direct pull is one request per connection) or, rarely, to ask
#: again. That wait is NOT ping-guarded: the puller is applying the tail of
#: the reply from its socket buffers and may be stalled or paused for longer
#: than the ping timeout, and a 1011 close here would abort the TCP
#: connection and discard the unread tail (reviewer auto-0925-123637,
#: 2026-09-29). It is bounded on wake, generously.
SERVE_IDLE_AFTER_REPLY_S = 900.0


async def wait_bounded_on_wake(awaitable, bound_s: float):
    """Await *awaitable* for at most ``bound_s`` seconds, judged ON WAKE:
    when the timer fires after a stall of this event loop, the awaitable is
    checked with done() first, so data that arrived during the stall wins
    over the deadline. Raises asyncio.TimeoutError otherwise. The bound is a
    property of the peer's silence, never of this loop's scheduling."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=bound_s)
        if task in done:
            return task.result()
        raise asyncio.TimeoutError()
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def serve_fleet_transport(
    *, token: str, recv, send, handler, close=None,
    authenticator: FleetAuthenticator,
    org_channel_for: "Callable[[str], OrgFleetAuthenticator | None] | None" = None,
    ping=None,
) -> None:
    """Serve the existing fleet handshake and records on carrier callbacks.

    The adapter owns connection lifetime, including failure/cancellation
    cleanup, just as DirectChannelServer does. ``close(code=, reason=)``
    is the transport's close, used to end an org-admitted connection whose
    persona left the newest adopted member set with CLOSE_MEMBERSHIP_STALE
    (4417), the code the registry uses for the same condition.

    ``ping`` is the transport's ping when the carrier has one (the direct
    listener): every wait for the peer's next request runs under
    :func:`wait_alive`, and every send is bounded by SERVE_SEND_STALL_S; a
    peer found dead or no longer reading is closed with 1011."""
    raw = await wait_alive(recv(), ping=ping)
    if raw is None:
        return
    admission = accept_client_hello(
        raw, session=token, authenticator=authenticator,
        org_channel_for=org_channel_for,
    )
    client_pub = admission.client_pub
    await send(tag_viewer_message(VIEWER_KIND_RECORD, admission.server_hello))
    crypto = ChannelCrypto.server(
        admission.private_key, admission.client_eph, admission.transcript,
    )
    # An org-admitted connection tells the handler which organization
    # admitted it; a personal one passes exactly what it always did.
    extra = {} if admission.org is None else {
        "authorize": admission.authorize, "admitted_org": admission.org,
    }

    async def authorized_handler(channel_token: str, message: bytes):
        # Re-resolve on every application message. A kick takes effect on
        # an already-open socket before any further data is accepted.
        admission.authorize(client_pub)
        response = handler(channel_token, message, client_pub, **extra)
        if inspect.isawaitable(response):
            response = await response
        return response

    replied = False

    async def guarded_recv():
        # Until the first reply is written the peer owes us a request, and
        # a ping-guarded wait catches a dead one. Once a reply has been
        # written the peer is applying it from its buffers; pinging it then
        # could cut off the tail (SERVE_IDLE_AFTER_REPLY_S).
        if not replied:
            return await wait_alive(recv(), ping=ping)
        try:
            return await wait_bounded_on_wake(recv(), SERVE_IDLE_AFTER_REPLY_S)
        except asyncio.TimeoutError:
            return None   # the peer never closed after its reply: end the channel quietly

    async def bounded_send(payload: bytes) -> None:
        nonlocal replied
        try:
            await asyncio.wait_for(send(payload), SERVE_SEND_STALL_S)
        except asyncio.TimeoutError:
            raise PeerUnresponsive(
                f"peer stopped reading: a send did not complete within {SERVE_SEND_STALL_S:g}s"
            ) from None
        replied = True

    try:
        await serve_established_channel(
            crypto,
            token=token,
            recv=guarded_recv,
            send=bounded_send,
            handler=authorized_handler,
        )
    except PeerUnresponsive as exc:
        logger.warning("fleet direct serve %s: %s", client_pub[:12], exc)
        if close is not None:
            with contextlib.suppress(Exception):
                await close(code=1011, reason=str(exc)[:120])
        raise
    except HandshakeError as exc:
        code = getattr(exc, "close_code", None)
        if code is None or close is None:
            raise
        with contextlib.suppress(Exception):
            await close(code=int(code), reason=str(exc)[:120])
        raise


async def authenticate_fleet_transport(
    transport,
    *,
    authenticator: "FleetAuthenticator | OrgFleetAuthenticator",
    expected_machine_pub: str,
    session: str,
) -> ViewerChannel:
    """Authenticate an already-open message transport as an exact fleet peer.

    Like ViewerChannel.authenticate, this consumes send/recv/close rather
    than choosing a carrier. The carrier establishes routing first; this
    handshake independently proves the expected durable machine identity.
    Each invocation creates fresh ephemeral keys and record sequence state.
    The caller owns the handshake deadline. Failure, including cancellation,
    closes the supplied transport before propagating.
    """
    try:
        private_key, hello = authenticator.build_client_hello(session, peer=expected_machine_pub)
        client_eph = json.loads(hello)["eph_pub"]
        await transport.send(hello)
        server_hello = await transport.recv()
        if isinstance(server_hello, str):
            raise HandshakeError("expected binary FLEET_SERVER_HELLO")
        server_eph, transcript = authenticator.verify_server(
            read_viewer_record(server_hello),
            session=session,
            client_eph=client_eph,
            expected_machine_pub=expected_machine_pub,
        )
        return ViewerChannel(
            transport, ChannelCrypto.client(private_key, server_eph, transcript)
        )
    except BaseException:
        with contextlib.suppress(Exception):
            result = transport.close()
            if inspect.isawaitable(result):
                await result
        raise


async def fleet_direct_connect(
    addr: str,
    *,
    authenticator: "FleetAuthenticator | OrgFleetAuthenticator",
    expected_machine_pub: str,
    session: str,
    timeout: float = 3.0,
) -> ViewerChannel:
    """Open one mutually authenticated fleet channel over a direct address.

    ``authenticator`` is the personal roster's for a machine of one's own
    fleet, or an org scope's OrgFleetAuthenticator for a co-member's
    machine; both produce a hello carrying ``eph_pub`` and verify the
    server's answer.
    """

    async def attempt() -> ViewerChannel:
        # No library keepalive (auto-fkqz6): its pong deadline killed every
        # pull longer than ~90 s, because the puller's own event loop is
        # busy applying and reads the pong late (SJC-2, 2026-09-29). The
        # frame waits run under wait_alive (pings only while waiting, judged
        # on wake), and the silence bounds remain the wedge detector.
        ws = await websockets.connect(
            addr, max_size=2**22, compression=None, open_timeout=timeout,
            ping_interval=None, ping_timeout=None,
        )
        try:
            await ws.send(json.dumps({"v": DIRECT_VERSION, "session": session}))
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        return await authenticate_fleet_transport(
            ws, authenticator=authenticator,
            expected_machine_pub=expected_machine_pub, session=session,
        )

    return await asyncio.wait_for(attempt(), timeout=timeout)
