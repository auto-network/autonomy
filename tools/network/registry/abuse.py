"""Bounded, process-local abuse controls for public relay links.

This module implements the policy frozen in ``graph://91e5e75f-7eb``.  Its
state is deliberately unsuitable as a browsing ledger: source, network, link,
and organization axes are independently keyed with a process-random secret;
there is no composite source-to-link key and nothing is durable.

The limiter is synchronous by construction.  A check and its charge complete
without an ``await``, so concurrent ASGI tasks cannot all observe one stale
counter value in the same event-loop process.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
import sys
import time
from array import array
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Mapping


logger = logging.getLogger(__name__)

UINT32_MAX = (1 << 32) - 1

COUNT_MIN_ROWS = 4
COUNT_MIN_WIDTH = 8_192
COUNT_MIN_SLICES = 13
COUNT_MIN_SLICE_SECONDS = 5.0


@dataclass(frozen=True)
class AdmissionLimit:
    burst: int
    sustained: int


@dataclass(frozen=True)
class ByteLimit:
    rate: float
    burst: float


ADMISSION_LIMITS: Mapping[str, AdmissionLimit] = {
    "source": AdmissionLimit(120, 600),
    "network": AdmissionLimit(600, 3_000),
    "link": AdmissionLimit(600, 3_000),
    "organization": AdmissionLimit(1_200, 6_000),
    "process": AdmissionLimit(2_400, 12_000),
}

ACTIVE_LIMITS: Mapping[str, int] = {
    "source": 64,
    "network": 192,
    "link": 128,
    "organization": 128,
    "process": 256,
}

BYTE_LIMITS: Mapping[str, ByteLimit] = {
    "channel": ByteLimit(8 * 1024 * 1024, 16 * 1024 * 1024),
    "source": ByteLimit(32 * 1024 * 1024, 64 * 1024 * 1024),
    "network": ByteLimit(64 * 1024 * 1024, 128 * 1024 * 1024),
    "link": ByteLimit(32 * 1024 * 1024, 64 * 1024 * 1024),
    "organization": ByteLimit(64 * 1024 * 1024, 128 * 1024 * 1024),
    "process": ByteLimit(128 * 1024 * 1024, 256 * 1024 * 1024),
}


class _ExactRing:
    """One exact rolling counter with the policy's two window queries."""

    def __init__(
        self,
        *,
        slices: int = COUNT_MIN_SLICES,
        slice_seconds: float = COUNT_MIN_SLICE_SECONDS,
    ) -> None:
        self._slice_count = slices
        self._slice_seconds = slice_seconds
        self._slices = array("I", [0]) * slices
        self._epoch: int | None = None

    def check_and_charge(
        self, now: float, limit: AdmissionLimit
    ) -> str | None:
        epoch = self._advance(now)
        burst = self._sum(epoch, 3)
        if burst >= limit.burst:
            return "burst"
        sustained = self._sum(epoch, self._slice_count)
        if sustained >= limit.sustained:
            return "sustained"
        slot = epoch % self._slice_count
        self._slices[slot] = min(UINT32_MAX, self._slices[slot] + 1)
        return None

    def _advance(self, now: float) -> int:
        epoch = math.floor(now / self._slice_seconds)
        if self._epoch is None:
            self._epoch = epoch
            return epoch
        if epoch <= self._epoch:
            return self._epoch
        elapsed = epoch - self._epoch
        if elapsed >= self._slice_count:
            self._slices = array("I", [0]) * self._slice_count
        else:
            for value in range(self._epoch + 1, epoch + 1):
                self._slices[value % self._slice_count] = 0
        self._epoch = epoch
        return epoch

    def _sum(self, epoch: int, count: int) -> int:
        return sum(
            self._slices[(epoch - offset) % self._slice_count]
            for offset in range(count)
        )


class _CountMinRing:
    """Packed, fixed-memory count-min estimates over rolling time slices."""

    def __init__(
        self,
        *,
        rows: int = COUNT_MIN_ROWS,
        width: int = COUNT_MIN_WIDTH,
        slices: int = COUNT_MIN_SLICES,
        slice_seconds: float = COUNT_MIN_SLICE_SECONDS,
    ) -> None:
        if rows != 4:
            raise ValueError("the digest-index contract requires exactly four rows")
        if width <= 0:
            raise ValueError("width must be positive")
        self._rows = rows
        self._width = width
        self._slice_count = slices
        self._slice_seconds = slice_seconds
        self._slice_size = rows * width
        self._slices = [
            array("I", [0]) * self._slice_size for _ in range(slices)
        ]
        self._epoch: int | None = None

    @property
    def allocated_bytes(self) -> int:
        """The packed steady-state allocation, including container overhead."""
        return sys.getsizeof(self._slices) + sum(
            sys.getsizeof(item) for item in self._slices
        )

    def check_and_charge(
        self, digest: bytes, now: float, limit: AdmissionLimit
    ) -> str | None:
        epoch = self._advance(now)
        indices = self._indices(digest)
        if self._estimate(epoch, 3, indices) >= limit.burst:
            return "burst"
        if self._estimate(epoch, self._slice_count, indices) >= limit.sustained:
            return "sustained"
        current = self._slices[epoch % self._slice_count]
        for row, column in enumerate(indices):
            index = row * self._width + column
            current[index] = min(UINT32_MAX, current[index] + 1)
        return None

    def _advance(self, now: float) -> int:
        epoch = math.floor(now / self._slice_seconds)
        if self._epoch is None:
            self._epoch = epoch
            return epoch
        if epoch <= self._epoch:
            return self._epoch
        elapsed = epoch - self._epoch
        if elapsed >= self._slice_count:
            self._slices = [
                array("I", [0]) * self._slice_size
                for _ in range(self._slice_count)
            ]
        else:
            for value in range(self._epoch + 1, epoch + 1):
                self._slices[value % self._slice_count] = (
                    array("I", [0]) * self._slice_size
                )
        self._epoch = epoch
        return epoch

    def _indices(self, digest: bytes) -> tuple[int, int, int, int]:
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("count-min key must be one SHA-256 digest")
        return tuple(
            int.from_bytes(digest[offset : offset + 8], "big") % self._width
            for offset in range(0, 32, 8)
        )  # type: ignore[return-value]

    def _estimate(
        self,
        epoch: int,
        slice_count: int,
        indices: tuple[int, int, int, int],
    ) -> int:
        estimates = []
        for row, column in enumerate(indices):
            index = row * self._width + column
            estimates.append(
                sum(
                    self._slices[(epoch - offset) % self._slice_count][index]
                    for offset in range(slice_count)
                )
            )
        return min(estimates)


class _TokenBucket:
    __slots__ = ("tokens", "updated_at", "refs", "delete_at")

    def __init__(self, limit: ByteLimit, now: float) -> None:
        self.tokens = float(limit.burst)
        self.updated_at = now
        self.refs = 0
        self.delete_at: float | None = None

    def refill(self, limit: ByteLimit, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(float(limit.burst), self.tokens + elapsed * limit.rate)
        self.updated_at = max(self.updated_at, now)

    def full_at(self, limit: ByteLimit, now: float) -> float:
        self.refill(limit, now)
        return now + max(0.0, float(limit.burst) - self.tokens) / limit.rate


@dataclass(frozen=True)
class AdmissionTicket:
    source: bytes
    network: bytes


@dataclass(frozen=True)
class ResolvedTicket:
    source: bytes
    network: bytes
    link: bytes
    organization: bytes


class ChannelLease:
    """Exact active and byte-rate ownership for one admitted public channel."""

    __slots__ = ("_limiter", "_ticket", "_channel_bucket", "_released")

    def __init__(
        self,
        limiter: "RelayAbuseLimiter",
        ticket: ResolvedTicket,
        channel_bucket: _TokenBucket,
    ) -> None:
        self._limiter = limiter
        self._ticket = ticket
        self._channel_bucket = channel_bucket
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def charge_bytes(self, size: int) -> bool:
        if self._released:
            return False
        return self._limiter._charge_bytes(self, size)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release(self)


class RelayAbuseLimiter:
    """The one process-local admission/concurrency/bandwidth policy."""

    _SCOPES = ("source", "network", "link", "organization")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        secret: bytes | None = None,
        admission_limits: Mapping[str, AdmissionLimit] = ADMISSION_LIMITS,
        active_limits: Mapping[str, int] = ACTIVE_LIMITS,
        byte_limits: Mapping[str, ByteLimit] = BYTE_LIMITS,
        rows: int = COUNT_MIN_ROWS,
        width: int = COUNT_MIN_WIDTH,
        slices: int = COUNT_MIN_SLICES,
        slice_seconds: float = COUNT_MIN_SLICE_SECONDS,
    ) -> None:
        self._clock = clock
        self._secret = secret or secrets.token_bytes(32)
        if len(self._secret) != 32:
            raise ValueError("limiter secret must be 32 bytes")
        self._admission_limits = dict(admission_limits)
        self._active_limits = dict(active_limits)
        self._byte_limits = dict(byte_limits)
        _validate_limits(
            self._admission_limits, self._active_limits, self._byte_limits
        )
        self._process_admission = _ExactRing(
            slices=slices, slice_seconds=slice_seconds
        )
        self._admission = {
            scope: _CountMinRing(
                rows=rows,
                width=width,
                slices=slices,
                slice_seconds=slice_seconds,
            )
            for scope in self._SCOPES
        }
        self._active_process = 0
        self._active: dict[str, dict[bytes, int]] = {
            scope: {} for scope in self._SCOPES
        }
        self._buckets: dict[str, dict[bytes, _TokenBucket]] = {
            scope: {} for scope in self._SCOPES
        }
        self._bucket_capacity = (
            self._active_limits["process"]
            + self._admission_limits["process"].burst
        )
        now = self._clock()
        self._process_bucket = _TokenBucket(self._byte_limits["process"], now)
        self._process_bucket.refs = 1
        self._decisions: Counter[tuple[str, str]] = Counter()

    @property
    def admission_allocated_bytes(self) -> int:
        return sum(ring.allocated_bytes for ring in self._admission.values())

    def begin(self, source_host: str) -> AdmissionTicket | None:
        """Charge process/source/network before any token or SQLite lookup."""
        now = self._clock()
        reason = self._process_admission.check_and_charge(
            now, self._admission_limits["process"]
        )
        if reason is not None:
            self._deny("process", reason)
            return None

        source_input, network_input = _source_inputs(source_host)
        source = self._digest("source", source_input)
        reason = self._admission["source"].check_and_charge(
            source, now, self._admission_limits["source"]
        )
        if reason is not None:
            self._deny("source", reason)
            return None
        network = self._digest("network", network_input)
        reason = self._admission["network"].check_and_charge(
            network, now, self._admission_limits["network"]
        )
        if reason is not None:
            self._deny("network", reason)
            return None
        self._allow("pre")
        return AdmissionTicket(source, network)

    def resolve(
        self, ticket: AdmissionTicket, bearer_token: str, organization: str
    ) -> ResolvedTicket | None:
        """Charge link then organization after the link resolves live."""
        now = self._clock()
        link = self._digest("link", bearer_token.encode("utf-8"))
        reason = self._admission["link"].check_and_charge(
            link, now, self._admission_limits["link"]
        )
        if reason is not None:
            self._deny("link", reason)
            return None
        org = self._digest("organization", organization.encode("utf-8"))
        reason = self._admission["organization"].check_and_charge(
            org, now, self._admission_limits["organization"]
        )
        if reason is not None:
            self._deny("organization", reason)
            return None
        self._allow("resolved")
        return ResolvedTicket(ticket.source, ticket.network, link, org)

    def acquire(self, ticket: ResolvedTicket) -> ChannelLease | None:
        """Atomically acquire every exact active count and byte bucket."""
        now = self._clock()
        self._purge_buckets(now)
        if self._active_process >= self._active_limits["process"]:
            self._deny("process", "active")
            return None
        keys = _ticket_keys(ticket)
        for scope, key in keys.items():
            if self._active[scope].get(key, 0) >= self._active_limits[scope]:
                self._deny(scope, "active")
                return None
            if key not in self._buckets[scope] and (
                len(self._buckets[scope]) >= self._bucket_capacity
            ):
                self._deny(scope, "bucket_capacity")
                return None

        self._active_process += 1
        for scope, key in keys.items():
            self._active[scope][key] = self._active[scope].get(key, 0) + 1
            bucket = self._buckets[scope].get(key)
            if bucket is None:
                bucket = _TokenBucket(self._byte_limits[scope], now)
                self._buckets[scope][key] = bucket
            bucket.refill(self._byte_limits[scope], now)
            bucket.refs += 1
            bucket.delete_at = None
        channel_bucket = _TokenBucket(self._byte_limits["channel"], now)
        channel_bucket.refs = 1
        self._allow("channel")
        return ChannelLease(self, ticket, channel_bucket)

    def snapshot(self) -> dict:
        """Bounded-cardinality aggregate state for tests and private metrics."""
        self._purge_buckets(self._clock())
        return {
            "active_process": self._active_process,
            "active_keys": {
                scope: len(values) for scope, values in self._active.items()
            },
            "byte_bucket_keys": {
                scope: len(values) for scope, values in self._buckets.items()
            },
            "decisions": {
                f"{scope}:{reason}": count
                for (scope, reason), count in sorted(self._decisions.items())
            },
        }

    def _charge_bytes(self, lease: ChannelLease, size: int) -> bool:
        if size < 0:
            raise ValueError("byte charge cannot be negative")
        if size == 0:
            return True
        now = self._clock()
        keys = _ticket_keys(lease._ticket)
        scoped = [
            ("channel", lease._channel_bucket),
            *(
                (scope, self._buckets[scope][key])
                for scope, key in keys.items()
            ),
            ("process", self._process_bucket),
        ]
        for scope, bucket in scoped:
            bucket.refill(self._byte_limits[scope], now)
            if bucket.tokens < size:
                self._deny(scope, "bytes")
                return False
        for _scope, bucket in scoped:
            bucket.tokens -= size
        return True

    def _release(self, lease: ChannelLease) -> None:
        now = self._clock()
        keys = _ticket_keys(lease._ticket)
        if self._active_process <= 0:
            raise RuntimeError("process active count underflow")
        self._active_process -= 1
        for scope, key in keys.items():
            count = self._active[scope].get(key)
            if count is None or count <= 0:
                raise RuntimeError(f"{scope} active count underflow")
            if count == 1:
                del self._active[scope][key]
            else:
                self._active[scope][key] = count - 1

            bucket = self._buckets[scope][key]
            if bucket.refs <= 0:
                raise RuntimeError(f"{scope} byte-bucket refcount underflow")
            bucket.refs -= 1
            if bucket.refs == 0:
                full_at = bucket.full_at(self._byte_limits[scope], now)
                if full_at <= now:
                    del self._buckets[scope][key]
                else:
                    bucket.delete_at = full_at

    def _purge_buckets(self, now: float) -> None:
        for scope, values in self._buckets.items():
            expired = [
                key
                for key, bucket in values.items()
                if bucket.refs == 0
                and bucket.delete_at is not None
                and bucket.delete_at <= now
            ]
            for key in expired:
                del values[key]

    def _digest(self, scope: str, value: bytes) -> bytes:
        return hmac.digest(
            self._secret, scope.encode("ascii") + b"\x00" + value, "sha256"
        )

    def _allow(self, phase: str) -> None:
        self._decisions[(phase, "allowed")] += 1

    def _deny(self, scope: str, reason: str) -> None:
        self._decisions[(scope, reason)] += 1
        logger.warning("relay abuse limiter denied: scope=%s reason=%s", scope, reason)


def _ticket_keys(ticket: ResolvedTicket) -> dict[str, bytes]:
    return {
        "source": ticket.source,
        "network": ticket.network,
        "link": ticket.link,
        "organization": ticket.organization,
    }


def _validate_limits(
    admission: Mapping[str, AdmissionLimit],
    active: Mapping[str, int],
    byte: Mapping[str, ByteLimit],
) -> None:
    scoped = {*RelayAbuseLimiter._SCOPES, "process"}
    if set(admission) != scoped or set(active) != scoped:
        raise ValueError("admission and active limits must cover every scope")
    if set(byte) != scoped | {"channel"}:
        raise ValueError("byte limits must cover every scope and channel")
    if any(
        limit.burst <= 0
        or limit.sustained <= 0
        or limit.burst > limit.sustained
        for limit in admission.values()
    ):
        raise ValueError("admission limits must be positive with burst <= sustained")
    if any(value <= 0 for value in active.values()):
        raise ValueError("active limits must be positive")
    if any(
        limit.rate <= 0 or limit.burst <= 0 or limit.burst < limit.rate
        for limit in byte.values()
    ):
        raise ValueError("byte limits must be positive with burst >= rate")


def _source_inputs(host: str) -> tuple[bytes, bytes]:
    """Canonical exact-source and source-network byte strings.

    Production receives an IP literal from the trusted loopback proxy.  ASGI
    test transports use the opaque hostname ``testclient``; treating any such
    non-IP peer as an opaque source keeps the production path testable without
    granting it a special unmetered bypass.
    """
    candidate = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        opaque = host.encode("utf-8", "surrogatepass")[:256]
        return b"opaque-exact\x00" + opaque, b"opaque-network\x00" + opaque
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address):
        exact = ipaddress.ip_network(f"{address}/32", strict=False)
        network = ipaddress.ip_network(f"{address}/24", strict=False)
    else:
        exact = ipaddress.ip_network(f"{address}/64", strict=False)
        network = ipaddress.ip_network(f"{address}/48", strict=False)
    return (
        bytes((exact.version, exact.prefixlen)) + exact.network_address.packed,
        bytes((network.version, network.prefixlen)) + network.network_address.packed,
    )
