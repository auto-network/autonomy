"""Strict, transport-independent validation for the bounded ICE exchange.

This module deliberately contains no aiortc import.  It is the signaling and
privacy boundary shared by the eventual dashboard responder and its fast unit
tests; the selected WebRTC implementation plugs in behind it only after the
dashboard image's licensing gate clears.

The exchange itself already rides inside an authenticated, encrypted
``relaykit.channel``.  Nothing here establishes application identity and no
ICE address, SDP, credential, or attempt identifier is logged or persisted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import inspect
import json
import re
import time
from typing import Any

from tools.network.idkit import canonical_json


ICE_SIGNAL_VERSION = 1
ICE_POLICIES = frozenset({"direct_allowed", "relay_only"})
MAX_CANDIDATES = 32
MAX_CANDIDATE_BYTES = 2 * 1024
MAX_SDP_BYTES = 64 * 1024
MAX_ATTEMPT_SIGNAL_BYTES = 128 * 1024
ATTEMPT_DEADLINE_SECONDS = 8.0

STUN_URL = "stun:turn.auto.network:3478"
TURN_URLS = (
    "turn:turn.auto.network:3478?transport=udp",
    "turn:turn.auto.network:3478?transport=tcp",
    "turns:turn.auto.network:443?transport=tcp",
)

_ATTEMPT_RE = re.compile(r"^[0-9a-f]{32}$")
_MDNS_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+local\.?$"
)
_CANDIDATE_FIELDS = frozenset(
    {"candidate", "sdpMid", "sdpMLineIndex", "usernameFragment"}
)
_SDP_CONNECTION_RE = re.compile(r"^(c=IN IP(4|6) )(\S+)(\r?\n)?$", re.IGNORECASE)


class IceSignalingError(ValueError):
    """The signaling connection must close while the artifact path survives."""


@dataclass(frozen=True)
class IceOffer:
    attempt_id: str
    sdp: str
    candidates: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class IceConfiguration:
    """The issuer-owned values returned by ``ice.config``.

    ``expires_at`` is explicit rather than inferred from the TURN-REST
    username. That keeps credential syntax out of the client wire contract and
    lets a relay-selected long session schedule one bounded ICE restart before
    the allocation can no longer refresh.
    """

    ice_servers: tuple[dict[str, Any], ...]
    expires_at: int


@dataclass(frozen=True)
class IceAnswer:
    sdp: str
    candidates: tuple[dict[str, Any], ...]


class IceCapacity:
    """Event-loop-local global and per-token caps for signaling attempts."""

    def __init__(self, limit: int, *, per_token_limit: int):
        if type(limit) is not int or limit <= 0:
            raise ValueError("ICE capacity limit must be positive")
        if (
            type(per_token_limit) is not int
            or per_token_limit <= 0
            or per_token_limit > limit
        ):
            raise ValueError("ICE per-token capacity must be within the global limit")
        self.limit = limit
        self.per_token_limit = per_token_limit
        self.active = 0
        self._by_token: dict[str, int] = {}

    def acquire(self, token: str) -> bool:
        token_active = self._by_token.get(token, 0)
        if self.active >= self.limit or token_active >= self.per_token_limit:
            return False
        self.active += 1
        self._by_token[token] = token_active + 1
        return True

    def release(self, token: str) -> None:
        token_active = self._by_token.get(token, 0)
        if self.active <= 0 or token_active <= 0:
            raise RuntimeError("ICE capacity released without an acquisition")
        self.active -= 1
        if token_active == 1:
            del self._by_token[token]
        else:
            self._by_token[token] = token_active - 1


def _wire_size(value: str) -> int:
    return len(value.encode("utf-8"))


def parse_message(raw: bytes | bytearray | memoryview) -> tuple[dict[str, Any], int]:
    """Decode one canonical signaling request with the total-attempt backstop.

    Canonical JSON is not required from the viewer—the encrypted application
    record already provides byte integrity—but duplicate keys are refused so a
    validator and a later consumer can never interpret different values.
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise IceSignalingError("signaling message must be bytes")
    wire = bytes(raw)
    if len(wire) > MAX_ATTEMPT_SIGNAL_BYTES:
        raise IceSignalingError("signaling attempt exceeds byte limit")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise IceSignalingError("signaling message has a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(wire.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise IceSignalingError("signaling message is not valid JSON") from exc
    if not isinstance(value, dict):
        raise IceSignalingError("signaling message must be an object")
    return value, len(wire)


def validate_begin(value: dict[str, Any], wire_bytes: int) -> str:
    if set(value) != {"v", "op", "attempt_id"}:
        raise IceSignalingError("ice.begin has an invalid field set")
    if type(value["v"]) is not int or value["v"] != ICE_SIGNAL_VERSION:
        raise IceSignalingError("unsupported ICE signaling version")
    if value["op"] != "ice.begin":
        raise IceSignalingError("expected ice.begin")
    attempt_id = value["attempt_id"]
    if not isinstance(attempt_id, str) or not _ATTEMPT_RE.fullmatch(attempt_id):
        raise IceSignalingError("attempt_id must be 32 lowercase hexadecimal characters")
    if wire_bytes > MAX_ATTEMPT_SIGNAL_BYTES:
        raise IceSignalingError("signaling attempt exceeds byte limit")
    return attempt_id


def _candidate_line_tokens(candidate: str) -> tuple[str, str, str | None, int | None]:
    tokens = candidate.split()
    if len(tokens) < 8 or not tokens[0].startswith("candidate:"):
        raise IceSignalingError("malformed ICE candidate")
    if not tokens[0][len("candidate:"):]:
        raise IceSignalingError("ICE candidate foundation is empty")
    try:
        component = int(tokens[1])
        priority = int(tokens[3])
        port = int(tokens[5])
    except ValueError as exc:
        raise IceSignalingError("ICE candidate numeric field is malformed") from exc
    if component not in (1, 2) or priority < 0 or not 1 <= port <= 65535:
        raise IceSignalingError("ICE candidate numeric field is out of range")
    if tokens[2].lower() not in ("udp", "tcp") or tokens[6] != "typ":
        raise IceSignalingError("ICE candidate transport or type marker is invalid")
    candidate_type = tokens[7].lower()
    if candidate_type not in ("host", "srflx", "relay"):
        raise IceSignalingError("ICE candidate type is not permitted")

    extensions = tokens[8:]
    if len(extensions) % 2:
        raise IceSignalingError("ICE candidate extensions are malformed")
    extension_map: dict[str, str] = {}
    for index in range(0, len(extensions), 2):
        key, val = extensions[index].lower(), extensions[index + 1]
        if key in extension_map:
            raise IceSignalingError("ICE candidate repeats an extension")
        extension_map[key] = val
    raddr = extension_map.get("raddr")
    rport_text = extension_map.get("rport")
    if (raddr is None) != (rport_text is None):
        raise IceSignalingError("ICE related address and port must appear together")
    rport = None
    if rport_text is not None:
        try:
            rport = int(rport_text)
        except ValueError as exc:
            raise IceSignalingError("ICE related port is malformed") from exc
        if not 1 <= rport <= 65535:
            raise IceSignalingError("ICE related port is out of range")
    return candidate_type, tokens[4], raddr, rport


def _public_ip(value: str, what: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise IceSignalingError(f"{what} must be an IP address") from exc
    if not address.is_global:
        raise IceSignalingError(f"{what} is not globally routable")
    return address


def validate_candidate(value: Any, policy: str) -> dict[str, Any]:
    """Validate one browser-shaped candidate without retaining its address."""
    if policy not in ICE_POLICIES:
        raise IceSignalingError("unknown ICE policy")
    if not isinstance(value, dict) or set(value) != _CANDIDATE_FIELDS:
        raise IceSignalingError("ICE candidate object has an invalid field set")
    candidate = value["candidate"]
    if not isinstance(candidate, str) or not candidate:
        raise IceSignalingError("ICE candidate line must be a non-empty string")
    if _wire_size(candidate) > MAX_CANDIDATE_BYTES:
        raise IceSignalingError("ICE candidate line exceeds byte limit")
    mid = value["sdpMid"]
    line_index = value["sdpMLineIndex"]
    ufrag = value["usernameFragment"]
    if mid is not None and (not isinstance(mid, str) or len(mid) > 256):
        raise IceSignalingError("ICE sdpMid is malformed")
    if line_index is not None and (
        type(line_index) is not int or not 0 <= line_index <= 65535
    ):
        raise IceSignalingError("ICE sdpMLineIndex is malformed")
    if ufrag is not None and (not isinstance(ufrag, str) or len(ufrag) > 256):
        raise IceSignalingError("ICE usernameFragment is malformed")

    candidate_type, address_text, related_text, related_port = (
        _candidate_line_tokens(candidate)
    )
    if policy == "relay_only" and candidate_type != "relay":
        raise IceSignalingError("relay-only policy received a direct candidate")
    if candidate_type == "host":
        # Literal host candidates are always dropped on public links—even a
        # currently public interface can expose topology that the product did
        # not promise to publish. Browser-obfuscated mDNS is the sole host form.
        if not _MDNS_RE.fullmatch(address_text):
            raise IceSignalingError("literal host candidate is not permitted")
        if related_text is not None:
            raise IceSignalingError("host candidate must not carry a related address")
    else:
        _public_ip(address_text, "ICE candidate address")
        if related_text is not None:
            try:
                related = ipaddress.ip_address(related_text)
            except ValueError as exc:
                raise IceSignalingError("ICE related address must be an IP address") from exc
            if not related.is_global and not (
                related.is_unspecified and related_port == 9
            ):
                raise IceSignalingError(
                    "private ICE related address was not privacy-scrubbed"
                )
    return value


def validate_candidates(values: Any, policy: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(values, list) or len(values) > MAX_CANDIDATES:
        raise IceSignalingError("ICE candidate array exceeds its limit")
    return tuple(validate_candidate(value, policy) for value in values)


def assert_candidate_free_sdp(sdp: Any) -> str:
    if not isinstance(sdp, str) or _wire_size(sdp) > MAX_SDP_BYTES:
        raise IceSignalingError("SDP is malformed or exceeds its byte limit")
    for line in sdp.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("a=candidate:"):
            raise IceSignalingError("SDP contains a smuggled ICE candidate")
        match = _SDP_CONNECTION_RE.match(stripped)
        if match and match.group(3) not in ("0.0.0.0", "::"):
            raise IceSignalingError("SDP contains a non-placeholder connection address")
    return sdp


def strip_candidate_lines(sdp: str) -> str:
    """Remove gathered candidates and neutralize SDP's other address field."""
    if not isinstance(sdp, str):
        raise IceSignalingError("SDP must be a string")
    clean_lines = []
    for line in sdp.splitlines(keepends=True):
        if line.strip().lower().startswith("a=candidate:"):
            continue
        match = _SDP_CONNECTION_RE.match(line)
        if match:
            placeholder = "0.0.0.0" if match.group(2) == "4" else "::"
            line = match.group(1) + placeholder + (match.group(4) or "")
        clean_lines.append(line)
    clean = "".join(clean_lines)
    return assert_candidate_free_sdp(clean)


def validate_offer(
    value: dict[str, Any],
    wire_bytes: int,
    *,
    attempt_id: str,
    policy: str,
    bytes_already_used: int = 0,
) -> IceOffer:
    if set(value) != {"v", "op", "attempt_id", "sdp", "candidates"}:
        raise IceSignalingError("ice.offer has an invalid field set")
    if type(value["v"]) is not int or value["v"] != ICE_SIGNAL_VERSION:
        raise IceSignalingError("unsupported ICE signaling version")
    if value["op"] != "ice.offer" or value["attempt_id"] != attempt_id:
        raise IceSignalingError("ICE offer does not match this attempt")
    if bytes_already_used + wire_bytes > MAX_ATTEMPT_SIGNAL_BYTES:
        raise IceSignalingError("signaling attempt exceeds byte limit")
    return IceOffer(
        attempt_id=attempt_id,
        sdp=assert_candidate_free_sdp(value["sdp"]),
        candidates=validate_candidates(value["candidates"], policy),
    )


def validate_ice_configuration(
    value: IceConfiguration, *, now: int
) -> IceConfiguration:
    """Validate the frozen browser-facing RTCIceServer-compatible shape."""
    if not isinstance(value, IceConfiguration):
        raise IceSignalingError("ICE configuration provider returned the wrong type")
    if type(value.expires_at) is not int or value.expires_at <= now:
        raise IceSignalingError("TURN credential is already expired")
    if len(value.ice_servers) != 2:
        raise IceSignalingError("ICE configuration must contain one STUN and one TURN entry")
    stun, turn = value.ice_servers
    if not isinstance(stun, dict) or stun != {"urls": [STUN_URL]}:
        raise IceSignalingError("ICE STUN entry does not match the frozen service")
    if not isinstance(turn, dict) or set(turn) != {
        "urls", "username", "credential", "credentialType"
    }:
        raise IceSignalingError("ICE TURN entry has an invalid field set")
    if turn["urls"] != list(TURN_URLS) or turn["credentialType"] != "password":
        raise IceSignalingError("ICE TURN URLs or credential type are invalid")
    for field in ("username", "credential"):
        item = turn[field]
        if not isinstance(item, str) or not item or _wire_size(item) > 512:
            raise IceSignalingError(f"ICE TURN {field} is malformed")
    return value


def _configuration_response(
    attempt_id: str, policy: str, configuration: IceConfiguration
) -> bytes:
    return canonical_json({
        "v": ICE_SIGNAL_VERSION,
        "op": "ice.config",
        "attempt_id": attempt_id,
        "policy": policy,
        "ice_servers": list(configuration.ice_servers),
        "expires_at": configuration.expires_at,
    })


class IceSignalingSession:
    """One bounded ICE attempt on one separately handshaken channel.

    ``configuration_provider(token, policy)`` returns an
    :class:`IceConfiguration`. ``responder_factory(configuration, policy)``
    returns an adapter exposing ``answer(offer, timeout=seconds)`` and
    ``aclose()``. Both may be async. The adapter seam is intentionally free of
    aiortc types so fast tests and the eventual LGPL-compatible runtime use the
    identical state machine.
    """

    def __init__(
        self,
        *,
        token: str,
        policy: str,
        configuration_provider,
        responder_factory,
        capacity: IceCapacity,
        monotonic=time.monotonic,
        wall_clock=time.time,
        deadline_seconds: float = ATTEMPT_DEADLINE_SECONDS,
    ):
        if policy not in ICE_POLICIES:
            raise ValueError("unknown ICE policy")
        if deadline_seconds <= 0:
            raise ValueError("ICE deadline must be positive")
        self._token = token
        self._policy = policy
        self._configuration_provider = configuration_provider
        self._responder_factory = responder_factory
        self._capacity = capacity
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._deadline_seconds = float(deadline_seconds)
        self._deadline_at = self._monotonic() + self._deadline_seconds
        self._state = "new"
        self._attempt_id: str | None = None
        self._started_at: float | None = None
        self._wire_bytes = 0
        self._configuration: IceConfiguration | None = None
        self._responder = None
        self._acquired = False
        self._transferred = False
        self._closed = False

    def _remaining(self) -> float:
        remaining = self._deadline_at - self._monotonic()
        if remaining <= 0:
            raise IceSignalingError("ICE attempt deadline elapsed")
        return remaining

    def receive_timeout(self) -> float:
        """Bound the next encrypted request even when the peer stays silent."""
        return self._remaining()

    def _account(self, wire: bytes) -> bytes:
        self._wire_bytes += len(wire)
        if self._wire_bytes > MAX_ATTEMPT_SIGNAL_BYTES:
            raise IceSignalingError("signaling attempt exceeds byte limit")
        return wire

    async def __call__(self, token: str, raw: bytes) -> bytes:
        if self._closed or token != self._token:
            raise IceSignalingError("ICE signaling channel is no longer usable")
        value, wire_bytes = parse_message(raw)
        if self._state == "new":
            attempt_id = validate_begin(value, wire_bytes)
            if not self._capacity.acquire(self._token):
                raise IceSignalingError("ICE responder capacity is exhausted")
            self._acquired = True
            self._attempt_id = attempt_id
            self._started_at = self._monotonic()
            self._deadline_at = self._started_at + self._deadline_seconds
            self._wire_bytes = wire_bytes
            self._remaining()
            configuration = self._configuration_provider(token, self._policy)
            if inspect.isawaitable(configuration):
                configuration = await asyncio.wait_for(
                    configuration, timeout=self._remaining()
                )
            self._configuration = validate_ice_configuration(
                configuration, now=int(self._wall_clock())
            )
            response = _configuration_response(
                attempt_id, self._policy, self._configuration
            )
            response = self._account(response)
            self._state = "config_ready"
            return response

        if self._state != "configured":
            raise IceSignalingError("ICE signaling attempt is already complete")
        assert self._attempt_id is not None and self._configuration is not None
        offer = validate_offer(
            value,
            wire_bytes,
            attempt_id=self._attempt_id,
            policy=self._policy,
            bytes_already_used=self._wire_bytes,
        )
        self._wire_bytes += wire_bytes
        responder = self._responder_factory(self._configuration, self._policy)
        if inspect.isawaitable(responder):
            responder = await asyncio.wait_for(responder, timeout=self._remaining())
        self._responder = responder
        answer = responder.answer(offer, timeout=self._remaining())
        if inspect.isawaitable(answer):
            answer = await asyncio.wait_for(answer, timeout=self._remaining())
        if not isinstance(answer, IceAnswer):
            raise IceSignalingError("ICE responder returned the wrong answer type")
        sdp = strip_candidate_lines(answer.sdp)
        candidates = validate_candidates(list(answer.candidates), self._policy)
        response = canonical_json({
            "v": ICE_SIGNAL_VERSION,
            "op": "ice.answer",
            "attempt_id": self._attempt_id,
            "sdp": sdp,
            "candidates": list(candidates),
        })
        response = self._account(response)
        self._state = "answer_ready"
        return response

    def on_response_sent(self) -> bool:
        """Confirm a complete encrypted response reached the transport.

        The terminal answer transfers the exact responder object—not the
        caller-chosen attempt id—to the direct-channel runtime.  Transfer is a
        synchronous, event-loop-local ownership change so cancellation cannot
        land between runtime adoption and the session recording it.  ``True``
        tells ``serve_channel`` to close the short-lived signaling channel.
        """
        if self._state == "config_ready":
            self._state = "configured"
            return False
        if self._state != "answer_ready" or self._responder is None:
            raise IceSignalingError("signaling response confirmation is out of order")
        transfer = getattr(self._responder, "transfer", None)
        if transfer is None:
            raise IceSignalingError("ICE responder cannot transfer ownership")
        result = transfer()
        if inspect.isawaitable(result) or result is not None:
            raise IceSignalingError("ICE responder ownership transfer must be synchronous")
        self._transferred = True
        self._state = "transferred"
        if self._acquired:
            self._capacity.release(self._token)
            self._acquired = False
        return True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._responder is not None and not self._transferred:
                close = getattr(self._responder, "aclose", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
        finally:
            if self._acquired:
                self._capacity.release(self._token)
                self._acquired = False
