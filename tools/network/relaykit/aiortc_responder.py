"""Pinned aiortc responder behind relaykit's library-neutral ICE seam.

The application and signaling protocols contain no aiortc objects.  This
module is the deliberately narrow implementation adapter selected in
``tools/network/ICE_STACK_SELECTION.md``.  It is imported only on user-side
installs: PyAV must not enter an Autonomy-published image.

Relay-only privacy reaches two private aioice attributes because aiortc 1.15
does not expose the browser's ``iceTransportPolicy`` setting.  The reach is
fail-closed and version-pinned.  In aioice 0.10.2, ``TransportPolicy.RELAY``
alone is insufficient: it suppresses advertised host candidates but still
gathers server-reflexive candidates and retains host sockets.  Passing an
empty address list to the pinned gather method prevents both while preserving
the independent TURN task, leaving only a relay protocol and candidate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from importlib import metadata
import ipaddress
import inspect
from typing import Any

from tools.network.idkit import DelegationCert, KeyPair

from .channel import MAX_RECORD_CHUNK_SIZE
from .connector import serve_channel
from .ice_signaling import (
    IceAnswer,
    IceConfiguration,
    IceOffer,
    IceSignalingError,
    STUN_URL,
    TURN_URLS,
    validate_candidate,
)


AIORTC_VERSION = "1.15.0"
AIOICE_VERSION = "0.10.2"
GATHER_TIMEOUT_SECONDS = 2
APPLICATION_HANDSHAKE_TIMEOUT_SECONDS = 15.0
DATA_CHANNEL_LOW_WATER_BYTES = 256 * 1024
DATA_CHANNEL_HIGH_WATER_BYTES = 512 * 1024
DATA_CHANNEL_DRAIN_TIMEOUT_SECONDS = 5.0
DATA_CHANNEL_LABEL = "autonomy-v1"
DATA_CHANNEL_QUEUE_RECORDS = 32
# 8-byte sequence, 1-byte encrypted flags, and a 16-byte GCM tag surround
# the largest record chunk the current protocol accepts.
MAX_DATA_CHANNEL_MESSAGE_BYTES = MAX_RECORD_CHUNK_SIZE + 25


class AiortcRuntimeError(RuntimeError):
    """The direct upgrade fails closed while the existing relay survives."""


@dataclass(frozen=True)
class _Modules:
    RTCPeerConnection: Any
    RTCConfiguration: Any
    RTCIceServer: Any
    RTCIceCandidate: Any
    RTCSessionDescription: Any
    candidate_from_sdp: Any
    candidate_to_sdp: Any
    TransportPolicy: Any


def load_aiortc_modules() -> _Modules:
    """Import and pin the exact implementation whose private seam we use."""
    try:
        aiortc_version = metadata.version("aiortc")
        aioice_version = metadata.version("aioice")
    except metadata.PackageNotFoundError as exc:
        raise AiortcRuntimeError(
            "the user-side WebRTC dependency is not installed"
        ) from exc
    if aiortc_version != AIORTC_VERSION or aioice_version != AIOICE_VERSION:
        raise AiortcRuntimeError(
            "the WebRTC dependency version is not the exercised version"
        )

    try:
        from aioice.ice import TransportPolicy
        from aiortc import (
            RTCConfiguration,
            RTCIceCandidate,
            RTCIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
    except Exception as exc:  # dependency is present but incomplete/broken
        raise AiortcRuntimeError("the WebRTC dependency cannot be loaded") from exc
    return _Modules(
        RTCPeerConnection=RTCPeerConnection,
        RTCConfiguration=RTCConfiguration,
        RTCIceServer=RTCIceServer,
        RTCIceCandidate=RTCIceCandidate,
        RTCSessionDescription=RTCSessionDescription,
        candidate_from_sdp=candidate_from_sdp,
        candidate_to_sdp=candidate_to_sdp,
        TransportPolicy=TransportPolicy,
    )


class PeerReservation:
    """One pre-answer slot, consumed synchronously by exact-object transfer."""

    def __init__(self, owner: "PeerRuntime", token: str):
        self.owner = owner
        self.token = token
        self.released = False
        self.adopted = False


class PeerRuntime:
    """Bound pending+established peers globally and per bearer token.

    Signaling attempts have a separate, shorter-lived cap.  This owner reserves
    a peer slot before an answer is built, so ownership transfer after the full
    answer send cannot fail and create an unowned live connection.
    """

    def __init__(self, limit: int, *, per_token_limit: int):
        if type(limit) is not int or limit <= 0:
            raise ValueError("peer limit must be positive")
        if (
            type(per_token_limit) is not int
            or per_token_limit <= 0
            or per_token_limit > limit
        ):
            raise ValueError("peer per-token limit must be within the global limit")
        self.limit = limit
        self.per_token_limit = per_token_limit
        self._reserved: set[PeerReservation] = set()
        self._peers: set[AiortcResponder] = set()
        self._by_token: dict[str, int] = {}

    @property
    def active(self) -> int:
        return len(self._reserved)

    @property
    def established(self) -> int:
        return sum(peer.established for peer in self._peers)

    def reserve(self, token: str) -> PeerReservation:
        count = self._by_token.get(token, 0)
        if self.active >= self.limit or count >= self.per_token_limit:
            raise IceSignalingError("ICE peer capacity is exhausted")
        reservation = PeerReservation(self, token)
        self._reserved.add(reservation)
        self._by_token[token] = count + 1
        return reservation

    def adopt(self, responder: "AiortcResponder", reservation: PeerReservation) -> None:
        if (
            reservation.owner is not self
            or reservation.released
            or reservation.adopted
            or reservation not in self._reserved
            or responder in self._peers
        ):
            raise RuntimeError("ICE peer ownership transfer is invalid")
        reservation.adopted = True
        self._peers.add(responder)

    def release_reservation(self, reservation: PeerReservation) -> None:
        """Release a slot before any responder exists to own it."""
        if reservation.adopted:
            raise RuntimeError("an adopted ICE peer must be released by identity")
        self._release(reservation)

    def release(self, responder: "AiortcResponder", reservation: PeerReservation) -> None:
        if reservation.released:
            return
        if reservation.owner is not self or reservation not in self._reserved:
            raise RuntimeError("ICE peer reservation is not owned by this runtime")
        if reservation.adopted and responder not in self._peers:
            raise RuntimeError("ICE peer identity was lost before release")
        self._peers.discard(responder)
        self._release(reservation)

    def _release(self, reservation: PeerReservation) -> None:
        if reservation.released:
            return
        if reservation.owner is not self or reservation not in self._reserved:
            raise RuntimeError("ICE peer reservation is not owned by this runtime")
        self._reserved.remove(reservation)
        reservation.released = True
        count = self._by_token.get(reservation.token, 0)
        if count <= 0:
            raise RuntimeError("ICE peer token count underflow")
        if count == 1:
            del self._by_token[reservation.token]
        else:
            self._by_token[reservation.token] = count - 1

    async def aclose(self) -> None:
        # Reserved-but-not-adopted responders remain owned by their signaling
        # sessions.  Runtime shutdown closes only the exact peers transferred
        # to it; those releases also remove their reservations.
        peers = list(self._peers)
        if peers:
            await asyncio.gather(*(peer.aclose() for peer in peers))


def _aiortc_configuration(configuration: IceConfiguration, modules: _Modules):
    """Select exactly one STUN and the frozen TURN/TLS URL for Python."""
    stun, turn = configuration.ice_servers
    if stun != {"urls": [STUN_URL]}:
        raise AiortcRuntimeError("the Python STUN configuration is ambiguous")
    turns_url = TURN_URLS[2]
    if turn.get("urls") != list(TURN_URLS) or turn.get("credentialType") != "password":
        raise AiortcRuntimeError("the Python TURN configuration is ambiguous")
    username, credential = turn.get("username"), turn.get("credential")
    if not isinstance(username, str) or not isinstance(credential, str):
        raise AiortcRuntimeError("the Python TURN credentials are malformed")
    return modules.RTCConfiguration(iceServers=[
        modules.RTCIceServer(urls=STUN_URL),
        modules.RTCIceServer(
            urls=turns_url,
            username=username,
            credential=credential,
            credentialType="password",
        ),
    ])


def _ice_connection(peer, modules: _Modules):
    """Return the pinned private aioice connection or fail closed."""
    try:
        sctp = peer.sctp
        ice = sctp.transport.transport
        gatherer = ice.iceGatherer
        connection = gatherer._connection
    except (AttributeError, TypeError) as exc:
        raise AiortcRuntimeError("aiortc's exercised ICE object path moved") from exc
    if (
        type(ice).__module__ != "aiortc.rtcicetransport"
        or type(ice).__name__ != "RTCIceTransport"
        or type(gatherer).__module__ != "aiortc.rtcicetransport"
        or type(gatherer).__name__ != "RTCIceGatherer"
        or type(connection).__module__ != "aioice.ice"
        or type(connection).__name__ != "Connection"
        or not callable(getattr(connection, "get_component_candidates", None))
        or not hasattr(connection, "_transport_policy")
        or not isinstance(getattr(connection, "_nominated", None), dict)
    ):
        raise AiortcRuntimeError("aiortc's exercised ICE private contract moved")
    return ice, gatherer, connection


def _pin_gathering(peer, policy: str, modules: _Modules) -> None:
    """Bound gathering and make relay-only mean TURN sockets only."""
    _ice, _gatherer, connection = _ice_connection(peer, modules)
    original = connection.get_component_candidates
    relay_only = policy == "relay_only"
    if policy not in ("direct_allowed", "relay_only"):
        raise AiortcRuntimeError("unknown ICE policy")
    if relay_only:
        connection._transport_policy = modules.TransportPolicy.RELAY

    async def bounded_candidates(*, component, addresses):
        chosen_addresses = [] if relay_only else addresses
        return await original(
            component=component,
            addresses=chosen_addresses,
            timeout=GATHER_TIMEOUT_SECONDS,
        )

    connection.get_component_candidates = bounded_candidates


def _assert_relay_selected(peer, modules: _Modules) -> None:
    """Prove the nominated local path is relay-only, not merely advertised so."""
    _ice, _gatherer, connection = _ice_connection(peer, modules)
    pairs = tuple(connection._nominated.values())
    if not pairs or any(pair.local_candidate.type != "relay" for pair in pairs):
        raise AiortcRuntimeError("relay-only ICE selected a direct local path")


def _remote_candidate(value: dict[str, Any], modules: _Modules):
    line = value["candidate"]
    parsed = modules.candidate_from_sdp(line[len("candidate:"):])
    parsed.sdpMid = value["sdpMid"]
    parsed.sdpMLineIndex = value["sdpMLineIndex"]
    return parsed


def _candidate_wire(candidate, *, mid: str, ufrag: str, policy: str, modules: _Modules):
    # Literal host candidates disclose local topology and are never emitted on
    # public links.  aiortc does not synthesize browser mDNS hostnames.
    if candidate.type == "host":
        return None
    if policy == "relay_only" and candidate.type != "relay":
        raise AiortcRuntimeError("relay-only gathering produced a direct candidate")

    # A relay candidate's related address is the responder's own TURN-facing
    # socket, not another usable route.  Always hide it, including when the
    # responder itself has a globally routable address.  Direct srflx
    # candidates retain a public related address because direct_allowed
    # explicitly permits disclosure of the responder's public address.
    related = candidate.relatedAddress
    if related is not None:
        try:
            related_ip = ipaddress.ip_address(related)
        except ValueError as exc:
            raise AiortcRuntimeError("aiortc produced an invalid related address") from exc
        if candidate.type == "relay" or not related_ip.is_global:
            candidate.relatedAddress = "::" if related_ip.version == 6 else "0.0.0.0"
            candidate.relatedPort = 9
    value = {
        "candidate": "candidate:" + modules.candidate_to_sdp(candidate),
        "sdpMid": mid,
        "sdpMLineIndex": 0,
        "usernameFragment": ufrag,
    }
    return validate_candidate(value, policy)


class AiortcResponder:
    """One answerer, session-owned until the encrypted answer is sent."""

    def __init__(
        self,
        *,
        token: str,
        policy: str,
        owner: PeerRuntime,
        reservation: PeerReservation,
        peer,
        key: KeyPair,
        cert: DelegationCert,
        org: str,
        application_handler,
        authorization_check=None,
        modules: _Modules,
        application_timeout: float = APPLICATION_HANDSHAKE_TIMEOUT_SECONDS,
    ):
        self.token = token
        self.policy = policy
        self._owner = owner
        self._reservation = reservation
        self._key = key
        self._cert = cert
        self._org = org
        self._handler = application_handler
        self._authorization_check = authorization_check
        self._modules = modules
        self._application_timeout = application_timeout
        self._peer = peer
        self._channel = None
        self._channel_task: asyncio.Task | None = None
        self._established = asyncio.Event()
        self._transferred = False
        self._closed = False
        self._watchdog: asyncio.Task | None = None
        self._peer.on("datachannel")(self._on_datachannel)
        self._peer.on("connectionstatechange")(self._on_connection_state_change)

    @property
    def established(self) -> bool:
        return self._established.is_set()

    async def answer(self, offer: IceOffer, *, timeout: float) -> IceAnswer:
        try:
            await self._peer.setRemoteDescription(
                self._modules.RTCSessionDescription(sdp=offer.sdp, type="offer")
            )
            if self._peer.sctp is None:
                raise AiortcRuntimeError("the offer contains no DataChannel transport")
            _pin_gathering(self._peer, self.policy, self._modules)
            for value in offer.candidates:
                await self._peer.addIceCandidate(_remote_candidate(value, self._modules))
            await self._peer.addIceCandidate(None)
            answer = await self._peer.createAnswer()
            await asyncio.wait_for(self._peer.setLocalDescription(answer), timeout=timeout)

            _ice, gatherer, _connection = _ice_connection(self._peer, self._modules)
            params = gatherer.getLocalParameters()
            mid = self._peer.sctp.mid
            if not isinstance(mid, str) or not isinstance(params.usernameFragment, str):
                raise AiortcRuntimeError("aiortc produced incomplete ICE parameters")
            candidates = []
            for candidate in gatherer.getLocalCandidates():
                wire = _candidate_wire(
                    candidate,
                    mid=mid,
                    ufrag=params.usernameFragment,
                    policy=self.policy,
                    modules=self._modules,
                )
                if wire is not None:
                    candidates.append(wire)
            if self.policy == "relay_only" and (
                not candidates
                or any(" typ relay" not in item["candidate"] for item in candidates)
            ):
                raise AiortcRuntimeError("relay-only gathering did not produce only relay candidates")
            return IceAnswer(
                sdp=self._peer.localDescription.sdp,
                candidates=tuple(candidates),
            )
        except BaseException:
            await self.aclose()
            raise

    def transfer(self) -> None:
        """Synchronously transfer this exact object after full answer send."""
        if self._closed or self._transferred:
            raise RuntimeError("ICE responder cannot be transferred")
        self._owner.adopt(self, self._reservation)
        self._transferred = True
        self._watchdog = asyncio.create_task(self._application_watchdog())

    async def _application_watchdog(self) -> None:
        try:
            await asyncio.wait_for(
                self._established.wait(), timeout=self._application_timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if not self._established.is_set():
                await self.aclose()

    def _on_datachannel(self, channel) -> None:
        if self._closed or self._channel is not None or channel.label != DATA_CHANNEL_LABEL:
            asyncio.create_task(self.aclose())
            return
        self._channel = channel
        self._channel_task = asyncio.create_task(self._serve_datachannel(channel))

    def _on_connection_state_change(self) -> None:
        state = self._peer.connectionState
        if state in ("connected", "completed") and self.policy == "relay_only":
            try:
                _assert_relay_selected(self._peer, self._modules)
            except Exception:
                asyncio.create_task(self.aclose())
        elif state in ("failed", "closed"):
            asyncio.create_task(self.aclose())

    async def _serve_datachannel(self, channel) -> None:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=DATA_CHANNEL_QUEUE_RECORDS
        )
        low = asyncio.Event()
        low.set()
        failed = False
        channel.bufferedAmountLowThreshold = DATA_CHANNEL_LOW_WATER_BYTES

        def fail_channel() -> None:
            nonlocal failed
            if not failed:
                failed = True
                asyncio.create_task(self.aclose())

        @channel.on("message")
        def on_message(message):
            if (
                not isinstance(message, bytes)
                or len(message) > MAX_DATA_CHANNEL_MESSAGE_BYTES
            ):
                fail_channel()
                return
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                fail_channel()

        @channel.on("close")
        def on_close():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                fail_channel()

        @channel.on("bufferedamountlow")
        def on_buffered_low():
            low.set()

        async def recv():
            return await queue.get()

        async def send(payload: bytes):
            if channel.readyState != "open":
                raise ConnectionError("WebRTC DataChannel is not open")
            if channel.bufferedAmount > DATA_CHANNEL_HIGH_WATER_BYTES:
                low.clear()
                if channel.bufferedAmount > DATA_CHANNEL_LOW_WATER_BYTES:
                    await asyncio.wait_for(
                        low.wait(), timeout=DATA_CHANNEL_DRAIN_TIMEOUT_SECONDS
                    )
            channel.send(payload)

        async def application_handler(token: str, message: bytes):
            # Reaching this wrapper proves the fresh root-pinned application
            # handshake succeeded. Re-check the local grant before declaring
            # the peer established: a link may have been revoked while ICE was
            # negotiating, and DTLS reachability is never authorization.
            if self._authorization_check is not None:
                allowed = self._authorization_check(token)
                if inspect.isawaitable(allowed):
                    allowed = await allowed
                if allowed is not True:
                    raise PermissionError("the local link grant is no longer valid")
            self._established.set()
            result = self._handler(token, message)
            if inspect.isawaitable(result):
                result = await result
            return result

        try:
            await serve_channel(
                self._key,
                self._cert,
                org=self._org,
                token=self.token,
                recv=recv,
                send=send,
                handler=application_handler,
            )
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        cancelled = []
        for task in (self._watchdog, self._channel_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
                cancelled.append(task)
        try:
            await self._peer.close()
        finally:
            try:
                if cancelled:
                    await asyncio.gather(*cancelled, return_exceptions=True)
            finally:
                self._owner.release(self, self._reservation)


class AiortcResponderFactory:
    """Token-bound responder factory consumed by ``IceSignalingSession``."""

    def __init__(
        self,
        *,
        token: str,
        owner: PeerRuntime,
        key: KeyPair,
        cert: DelegationCert,
        org: str,
        application_handler,
        authorization_check=None,
        modules: _Modules | None = None,
    ):
        self.token = token
        self.owner = owner
        self.key = key
        self.cert = cert
        self.org = org
        self.application_handler = application_handler
        self.authorization_check = authorization_check
        self.modules = modules or load_aiortc_modules()
        # xw5ow: persona belongs only in the registry-admission certificate.
        # A viewer SERVER_HELLO may carry only the direct-root neutral twin.
        if (
            cert.child_pub != key.public_hex
            or cert.org != org
            or tuple(cert.scope) != ("tunnel:serve",)
            or cert.parent_cert is not None
            or cert.subject.kind != "operator"
            or cert.subject.id != cert.child_pub
        ):
            raise AiortcRuntimeError(
                "the DataChannel requires the direct-root identity-neutral "
                "viewer certificate for this serving key and organization"
            )

    async def __call__(
        self, configuration: IceConfiguration, policy: str
    ) -> AiortcResponder:
        reservation = self.owner.reserve(self.token)
        peer = None
        try:
            peer = self.modules.RTCPeerConnection(
                _aiortc_configuration(configuration, self.modules)
            )
            return AiortcResponder(
                token=self.token,
                policy=policy,
                owner=self.owner,
                reservation=reservation,
                peer=peer,
                key=self.key,
                cert=self.cert,
                org=self.org,
                application_handler=self.application_handler,
                authorization_check=self.authorization_check,
                modules=self.modules,
            )
        except BaseException:
            # Peer construction is an async resource boundary.  If any later
            # constructor step fails, close that exact peer before returning
            # the reservation; never rely on garbage collection to clean it.
            try:
                if peer is not None:
                    await asyncio.shield(peer.close())
            finally:
                self.owner.release_reservation(reservation)
            raise
