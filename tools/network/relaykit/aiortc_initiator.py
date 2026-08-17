"""Native WebRTC offerer for an existing RelayKit application channel.

Signaling already runs inside a separately authenticated ``ViewerChannel``.
After ICE selects a path, this module authenticates a fresh application
``ViewerChannel`` over the ordered DataChannel.  SDP, candidates, TURN
credentials, and aiortc objects never reach the application handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass

from tools.network.idkit import canonical_json

from .aiortc_responder import (
    DATA_CHANNEL_DRAIN_TIMEOUT_SECONDS,
    DATA_CHANNEL_HIGH_WATER_BYTES,
    DATA_CHANNEL_LABEL,
    DATA_CHANNEL_LOW_WATER_BYTES,
    DATA_CHANNEL_QUEUE_RECORDS,
    MAX_DATA_CHANNEL_MESSAGE_BYTES,
    MAX_DATA_CHANNEL_OUTBOUND_BYTES,
    _aiortc_configuration,
    _assert_relay_selected,
    _candidate_wire,
    _ice_connection,
    _pin_gathering,
    _remote_candidate,
    load_aiortc_modules,
)
from .ice_signaling import (
    ATTEMPT_DEADLINE_SECONDS,
    ICE_POLICIES,
    ICE_SIGNAL_VERSION,
    MAX_ATTEMPT_SIGNAL_BYTES,
    IceAnswer,
    IceConfiguration,
    IceOffer,
    IceSignalingError,
    assert_candidate_free_sdp,
    parse_message,
    strip_candidate_lines,
    validate_candidates,
    validate_ice_configuration,
)
from .viewer import ViewerChannel


class NativeIceError(RuntimeError):
    """The native upgrade failed; the caller's Relay path remains usable."""


def _attempt_id() -> str:
    return secrets.token_hex(16)


def _configuration(
    raw: bytes, *, attempt_id: str, now: int
) -> tuple[str, IceConfiguration, int]:
    value, wire_bytes = parse_message(raw)
    if wire_bytes > MAX_ATTEMPT_SIGNAL_BYTES or set(value) != {
        "v", "op", "attempt_id", "policy", "ice_servers", "expires_at"
    }:
        raise IceSignalingError("ice.config has an invalid field set")
    if (
        type(value["v"]) is not int
        or value["v"] != ICE_SIGNAL_VERSION
        or value["op"] != "ice.config"
        or value["attempt_id"] != attempt_id
        or not isinstance(value["policy"], str)
        or value["policy"] not in ICE_POLICIES
        or not isinstance(value["ice_servers"], list)
    ):
        raise IceSignalingError("ice.config does not match this attempt")
    configuration = IceConfiguration(
        ice_servers=tuple(value["ice_servers"]),
        expires_at=value["expires_at"],
    )
    return (
        value["policy"],
        validate_ice_configuration(configuration, now=now),
        wire_bytes,
    )


def _answer(raw: bytes, *, attempt_id: str, policy: str) -> tuple[IceAnswer, int]:
    value, wire_bytes = parse_message(raw)
    if wire_bytes > MAX_ATTEMPT_SIGNAL_BYTES or set(value) != {
        "v", "op", "attempt_id", "sdp", "candidates"
    }:
        raise IceSignalingError("ice.answer has an invalid field set")
    if (
        type(value["v"]) is not int
        or value["v"] != ICE_SIGNAL_VERSION
        or value["op"] != "ice.answer"
        or value["attempt_id"] != attempt_id
    ):
        raise IceSignalingError("ice.answer does not match this attempt")
    return (
        IceAnswer(
            sdp=assert_candidate_free_sdp(value["sdp"]),
            candidates=validate_candidates(value["candidates"], policy),
        ),
        wire_bytes,
    )


class _DataChannelTransport:
    """The small async transport surface consumed by ``ViewerChannel``."""

    def __init__(self, channel, peer):
        self._channel = channel
        self._peer = peer
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=DATA_CHANNEL_QUEUE_RECORDS
        )
        self._low = asyncio.Event()
        self._low.set()
        self._closed = False
        self._close_task: asyncio.Task | None = None
        channel.bufferedAmountLowThreshold = DATA_CHANNEL_LOW_WATER_BYTES

        @channel.on("message")
        def on_message(message):
            if (
                self._closed
                or not isinstance(message, bytes)
                or len(message) > MAX_DATA_CHANNEL_MESSAGE_BYTES
            ):
                self._schedule_close()
                return
            try:
                self._queue.put_nowait(message)
            except asyncio.QueueFull:
                self._schedule_close()

        @channel.on("close")
        def on_close():
            self._schedule_close()

        @channel.on("bufferedamountlow")
        def on_buffered_low():
            self._low.set()

    def _schedule_close(self) -> None:
        if self._close_task is not None:
            return
        self._closed = True
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
        self._close_task = asyncio.create_task(self._close_objects())

    async def _close_objects(self) -> None:
        with contextlib.suppress(Exception):
            self._channel.close()
        with contextlib.suppress(Exception):
            await self._peer.close()

    async def send(self, payload: bytes) -> None:
        if (
            self._closed
            or not isinstance(payload, bytes)
            or len(payload) > MAX_DATA_CHANNEL_OUTBOUND_BYTES
            or self._channel.readyState != "open"
        ):
            raise ConnectionError("native DataChannel is not writable")
        if self._channel.bufferedAmount > DATA_CHANNEL_HIGH_WATER_BYTES:
            self._low.clear()
            if self._channel.bufferedAmount > DATA_CHANNEL_LOW_WATER_BYTES:
                await asyncio.wait_for(
                    self._low.wait(), timeout=DATA_CHANNEL_DRAIN_TIMEOUT_SECONDS
                )
        self._channel.send(payload)

    async def recv(self) -> bytes:
        if self._closed:
            raise ConnectionError("native DataChannel is closed")
        value = await self._queue.get()
        if value is None:
            raise ConnectionError("native DataChannel closed")
        return value

    async def close(self) -> None:
        self._schedule_close()
        await self._close_task


@dataclass
class NativeIceChannel:
    """An authenticated native application channel and its ICE metadata."""

    channel: ViewerChannel
    policy: str
    expires_at: int
    peer: object

    async def close(self) -> None:
        await self.channel.close()

    async def __aenter__(self) -> "NativeIceChannel":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def _wait_open(channel, *, timeout: float) -> None:
    opened = asyncio.Event()
    failed = asyncio.Event()

    @channel.on("open")
    def on_open():
        opened.set()

    @channel.on("close")
    def on_close():
        failed.set()

    # Re-check after registering callbacks so an open transition cannot land
    # in the small gap between the initial state read and listener setup.
    if channel.readyState == "open":
        return

    async def wait():
        while not opened.is_set():
            if failed.is_set():
                raise ConnectionError("native DataChannel closed before opening")
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=timeout)


async def upgrade_via_ice(
    signaling: ViewerChannel,
    *,
    token: str,
    root_pub: str,
    org: str,
    now=None,
    timeout: float = ATTEMPT_DEADLINE_SECONDS,
) -> NativeIceChannel:
    """Offer one bounded native upgrade over a dedicated signal channel.

    The caller's ordinary Relay application channel remains its fallback.  The
    separate ``signaling`` channel is always closed once this one-shot exchange
    ends, matching the browser initiator and releasing server-side capacity.
    """
    if timeout <= 0:
        raise ValueError("ICE timeout must be positive")
    attempt_id = _attempt_id()
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise NativeIceError("native ICE attempt timed out")
        return value

    peer = None
    transport = None
    try:
        modules = load_aiortc_modules()
        begin_wire = canonical_json(
            {
                "v": ICE_SIGNAL_VERSION,
                "op": "ice.begin",
                "attempt_id": attempt_id,
            }
        )
        wire_bytes = len(begin_wire)
        await asyncio.wait_for(
            signaling.send_message(begin_wire),
            timeout=remaining(),
        )
        raw_config = await asyncio.wait_for(
            signaling.recv_message(), timeout=remaining()
        )
        policy, configuration, config_bytes = _configuration(
            raw_config,
            attempt_id=attempt_id,
            now=int(time.time()) if now is None else int(now),
        )
        wire_bytes += config_bytes
        peer = modules.RTCPeerConnection(
            _aiortc_configuration(configuration, modules)
        )
        data_channel = peer.createDataChannel(DATA_CHANNEL_LABEL, ordered=True)
        _pin_gathering(peer, policy, modules)
        offer = await peer.createOffer()
        await asyncio.wait_for(peer.setLocalDescription(offer), timeout=remaining())
        _ice, gatherer, _connection = _ice_connection(peer, modules)
        params = gatherer.getLocalParameters()
        mid = peer.sctp.mid
        if not isinstance(mid, str) or not isinstance(params.usernameFragment, str):
            raise NativeIceError("native ICE parameters are incomplete")
        candidates = []
        for candidate in gatherer.getLocalCandidates():
            value = _candidate_wire(
                candidate,
                mid=mid,
                ufrag=params.usernameFragment,
                policy=policy,
                modules=modules,
            )
            if value is not None:
                candidates.append(value)
        candidates = list(validate_candidates(candidates, policy))
        if policy == "relay_only" and not candidates:
            raise NativeIceError("native relay-only gathering produced no candidate")
        offer_value = IceOffer(
            attempt_id=attempt_id,
            sdp=strip_candidate_lines(peer.localDescription.sdp),
            candidates=tuple(candidates),
        )
        offer_wire = canonical_json(
            {
                "v": ICE_SIGNAL_VERSION,
                "op": "ice.offer",
                "attempt_id": offer_value.attempt_id,
                "sdp": offer_value.sdp,
                "candidates": list(offer_value.candidates),
            }
        )
        wire_bytes += len(offer_wire)
        if wire_bytes > MAX_ATTEMPT_SIGNAL_BYTES:
            raise IceSignalingError("signaling attempt exceeds byte limit")
        await asyncio.wait_for(
            signaling.send_message(offer_wire),
            timeout=remaining(),
        )
        raw_answer = await asyncio.wait_for(
            signaling.recv_message(), timeout=remaining()
        )
        answer, answer_bytes = _answer(
            raw_answer, attempt_id=attempt_id, policy=policy
        )
        if wire_bytes + answer_bytes > MAX_ATTEMPT_SIGNAL_BYTES:
            raise IceSignalingError("signaling attempt exceeds byte limit")
        await peer.setRemoteDescription(
            modules.RTCSessionDescription(sdp=answer.sdp, type="answer")
        )
        for value in answer.candidates:
            await peer.addIceCandidate(_remote_candidate(value, modules))
        await peer.addIceCandidate(None)
        await _wait_open(data_channel, timeout=remaining())
        if policy == "relay_only":
            _assert_relay_selected(peer, modules)
        transport = _DataChannelTransport(data_channel, peer)
        application = await asyncio.wait_for(
            ViewerChannel.authenticate(
                transport,
                token,
                root_pub=root_pub,
                org=org,
                now=now,
            ),
            timeout=remaining(),
        )
        return NativeIceChannel(
            channel=application,
            policy=policy,
            expires_at=configuration.expires_at,
            peer=peer,
        )
    except BaseException:
        if transport is not None:
            await transport.close()
        elif peer is not None:
            await peer.close()
        raise
    finally:
        await signaling.close()
