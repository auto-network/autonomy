"""Fountain-coded artifact store — RaptorQ symbols, not blocks (§8 rev 2).

An artifact is a single hash-committed byte object moved as **RaptorQ
encoded symbols** (RFC 6330, via the vetted ``raptorq`` crate's PyO3
bindings — the codec is never reimplemented here). The manifest names
the object hash, size, and symbol size; the artifact id is the SHA-256
of the canonical-JSON manifest, exactly like the block manifest before
it — but there is no per-block hash list, because coded symbols cannot
be verified one at a time against a fixed digest. Integrity moves to
the *decoded object*: reconstruct from any K+ε symbols, hash, and fail
closed on mismatch (see ``fountain_fetch`` for polluter
identification).

Why symbols beat blocks (the auto-25dz3 review): a block scheduler's
"publisher uploads ≈1×" depends on have-maps propagating faster than
the publisher drains want-lists — a latency accident, measured failing
(3×) on zero-latency links. Fountain symbols are interchangeable, so a
seeder never needs to know what a leecher *has* to be useful; it only
needs to emit symbols nobody else is emitting:

- **Monotonic per-artifact cursor** — a complete seeder never serves
  the same fresh symbol twice, so its egress for an artifact is the
  count of *distinct* symbols it contributed, regardless of link
  latency. Different leechers polling one seeder receive complementary
  slices and complete by trading — the ≈1× property is structural
  (per seeder; swarm-wide it additionally needs collision-free
  stripes, see below).
- **Stripes** — the codec's Python bindings only generate the packet
  stream as a prefix (there is no arbitrary-ESI repair API), and the
  stream is deterministic in ``(data, symbol_size)``, so two complete
  seeders would emit *identical* symbols. Each seeder therefore owns
  stream positions ``p ≡ stripe (mod n_stripes)`` — interleaved, so a
  stripe's cost stays O(symbols served × n_stripes) encode throughput
  (measured ~800 MB/s marginal; ~0.6 s per 50 MB served at the default
  8 stripes) instead of O(stripe base). Callers derive the stripe from
  the org roster (:func:`roster_stripe`): the ledger's member ordering
  is the same everywhere, so assignment needs no runtime coordination.
- **Exclude ranges** — requests carry the leecher's holdings as packed
  ``(SBN, ESI)`` id ranges; nothing already held crosses the wire.
  Partial holders (leechers trading mid-download) serve their stored
  packets round-robin against the same exclude.

v1 boundaries, deliberate: pools and objects are in-memory (extending
a pool transiently materialises the stream prefix — fine at tens-of-MB
artifact sizes, a disk-backed store slots behind the same surface
later); symbols are not individually signed (authenticated-member
pollution handling is decode-verify + bisect, publisher-signed symbol
commitments are the v2 hardening).
"""

from __future__ import annotations

import bisect
import hashlib
import threading
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .store import BlockError, manifest_id

try:  # pragma: no cover - import guard exercised only where the wheel is absent
    from raptorq import Decoder, Encoder
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "swarmkit.fountain needs the 'raptorq' PyPI package "
        "(PyO3 bindings of the Apache-2.0 cberner/raptorq crate); "
        "pin: raptorq==2.0.0"
    ) from exc

FOUNTAIN_VERSION = 1
DEFAULT_SYMBOL_SIZE = 8192          # fits the E2E channel; ~420 MB/s codec at 50 MB
DEFAULT_N_STRIPES = 8
PACKET_HEADER = 4                   # RFC 6330 payload id: SBN u8 ‖ ESI u24 (BE)
MAX_SYMBOL_SIZE = 65535             # the codec's MTU is a u16
MAX_STREAM = 1 << 22                # pool positions a seeder will ever generate
MAX_PACKED = 1 << 32


class FountainError(BlockError):
    """A fountain manifest, packet, or range failed validation."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hex64(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


# ── manifest ────────────────────────────────────────────────────────────


def build_fountain_manifest(data: bytes,
                            symbol_size: int = DEFAULT_SYMBOL_SIZE) -> dict:
    """Describe *data* for fountain transfer: hash commitment, no block list."""
    if not data:
        raise FountainError("empty artifacts have no manifest")
    if not 0 < symbol_size <= MAX_SYMBOL_SIZE:
        raise FountainError("symbol_size out of range")
    return {
        "v": FOUNTAIN_VERSION,
        "kind": "fountain",
        "algo": "sha256",
        "size": len(data),
        "symbol_size": symbol_size,
        "object": _sha256_hex(data),
    }


def check_fountain_manifest(manifest: object) -> dict:
    """Validate manifest shape; returns it or raises :class:`FountainError`."""
    if not isinstance(manifest, dict):
        raise FountainError("manifest must be an object")
    if set(manifest) != {"v", "kind", "algo", "size", "symbol_size", "object"}:
        raise FountainError("manifest has wrong fields")
    if manifest["v"] != FOUNTAIN_VERSION:
        raise FountainError("unsupported manifest version")
    if manifest["kind"] != "fountain":
        raise FountainError("not a fountain manifest")
    if manifest["algo"] != "sha256":
        raise FountainError("unsupported hash algorithm")
    size, symbol_size = manifest["size"], manifest["symbol_size"]
    if not isinstance(size, int) or not isinstance(symbol_size, int):
        raise FountainError("size fields must be integers")
    if size <= 0 or not 0 < symbol_size <= MAX_SYMBOL_SIZE:
        raise FountainError("size fields out of range")
    if source_symbols(manifest) > MAX_STREAM:
        raise FountainError("object too large for symbol size")
    if not _hex64(manifest["object"]):
        raise FountainError("object hash must be 64 lowercase hex chars")
    return manifest


def fountain_id(manifest: dict) -> str:
    """The artifact id: SHA-256 over the canonical-JSON manifest."""
    return manifest_id(manifest)


def source_symbols(manifest: dict) -> int:
    """K — the source symbol count; batch sizing, not a completion bound."""
    size, symbol_size = manifest["size"], manifest["symbol_size"]
    return (size + symbol_size - 1) // symbol_size


def packet_length(manifest: dict) -> int:
    """Every serialized packet is payload id + one full symbol."""
    return PACKET_HEADER + manifest["symbol_size"]


def packet_id(packet: bytes) -> int:
    """Packed 32-bit ``SBN<<24 | ESI`` from the packet's payload id."""
    return int.from_bytes(packet[:PACKET_HEADER], "big")


# ── holdings as packed-id ranges ────────────────────────────────────────


def ids_to_ranges(ids: Iterable[int]) -> List[List[int]]:
    """Compress a packed-id set into sorted disjoint ``[start, end)`` pairs."""
    out: List[List[int]] = []
    for i in sorted(ids):
        if out and i == out[-1][1]:
            out[-1][1] = i + 1
        elif not out or i > out[-1][1]:
            out.append([i, i + 1])
    return out


def check_ranges(ranges: object) -> List[Tuple[int, int]]:
    """Validate wire-form ranges; returns tuples or raises."""
    if not isinstance(ranges, list):
        raise FountainError("exclude must be a list of ranges")
    out: List[Tuple[int, int]] = []
    last = -1
    for pair in ranges:
        if (
            not isinstance(pair, (list, tuple)) or len(pair) != 2
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in pair)
        ):
            raise FountainError("ranges must be [start, end] integer pairs")
        start, end = pair
        if not 0 <= start < end <= MAX_PACKED:
            raise FountainError("range bounds out of order")
        if start <= last:
            raise FountainError("ranges must be sorted and disjoint")
        out.append((start, end))
        last = end - 1
    return out


def in_ranges(ranges: Sequence[Tuple[int, int]], packed: int) -> bool:
    """Membership by bisect over validated ``(start, end)`` tuples."""
    i = bisect.bisect_right(ranges, (packed, MAX_PACKED))
    return i > 0 and ranges[i - 1][0] <= packed < ranges[i - 1][1]


# ── striping ────────────────────────────────────────────────────────────


def roster_stripe(pub_hex: str, roster: Iterable[str],
                  n_stripes: int = DEFAULT_N_STRIPES) -> int:
    """This member's stripe from the org roster's stable ordering.

    Every member sorts the same roster, so assignment is identical
    everywhere with no runtime coordination; stripes are collision-free
    while the roster holds ≤ *n_stripes* members and recycle beyond
    (exclude ranges absorb the residual overlap).
    """
    ordered = sorted(set(roster) | {pub_hex})
    return ordered.index(pub_hex) % n_stripes


# Beyond n_stripes concurrent seeders, two share a stripe: their
# independent cursors emit the same positions, so a leecher may be
# offered symbols it already holds (skipped by its exclude ranges) or
# receive a duplicate across concurrent requests (dropped by
# ``add_packet``). Pure bandwidth waste, never wrong bytes — decode
# correctness and the ≈1× *distinct*-symbol union are untouched, but
# TOTAL serving bandwidth degrades to ~N_collision× (validated under
# forced collisions + delayed responses): the ≈1× total-egress claim
# holds only under collision-free striping. Raise ``n_stripes``
# (roster-wide, so every member derives the same assignment) when
# swarms outgrow the default; dynamic reassignment on detected
# duplicate serving is the v2 lever.


class _StripePool:
    """The deterministic packet stream, filtered to one seeder's stripe.

    The bindings generate the stream only as a prefix, so the pool runs
    a doubling frontier: each extension re-materialises the prefix,
    keeps positions ``≡ stripe (mod n_stripes)`` past the old frontier,
    and drops the rest. ``take`` pops monotonically — a served (or
    skipped-as-held) packet is never offered again, which is the whole
    egress bound.
    """

    def __init__(self, data: bytes, symbol_size: int, stripe: int,
                 n_stripes: int):
        self._encoder = Encoder.with_defaults(data, symbol_size)
        self._source = (len(data) + symbol_size - 1) // symbol_size
        self._stripe = stripe % n_stripes
        self._n = n_stripes
        self._frontier = 0            # stream positions consumed so far
        self._pending: List[bytes] = []
        self.served = 0               # distinct packets handed out

    def _extend(self) -> bool:
        target = max(self._source, self._frontier * 2, 256)
        if self._frontier >= MAX_STREAM:
            return False
        target = min(target, MAX_STREAM)
        stream = self._encoder.get_encoded_packets(max(0, target - self._source))
        chunk = stream[self._frontier:target]
        if not chunk:
            return False
        first = self._frontier
        self._pending.extend(
            pkt for offset, pkt in enumerate(chunk)
            if (first + offset) % self._n == self._stripe
        )
        self._frontier = first + len(chunk)
        return True

    def take(self, count: int,
             exclude: Sequence[Tuple[int, int]]) -> List[bytes]:
        out: List[bytes] = []
        cursor = 0
        while len(out) < count:
            if cursor >= len(self._pending):
                del self._pending[:cursor]
                cursor = 0
                if not self._extend():
                    break
                continue
            pkt = self._pending[cursor]
            cursor += 1
            if in_ranges(exclude, packet_id(pkt)):
                continue              # requester holds it; drop from rotation
            out.append(pkt)
        del self._pending[:cursor]
        self.served += len(out)
        return out


# ── store ───────────────────────────────────────────────────────────────


class FountainStore:
    """Manifests, complete objects, and received packets per artifact.

    Complete holders serve *fresh* stripe symbols through a monotonic
    pool; partial holders serve their *stored* packets round-robin.
    Both paths honor the requester's exclude ranges. The stripe is
    node identity (one store per node), derived from the roster by the
    caller — two complete seeders on the same stripe waste bandwidth
    but break nothing.
    """

    def __init__(self, *, stripe: int = 0,
                 n_stripes: int = DEFAULT_N_STRIPES):
        if not 0 <= stripe < n_stripes:
            raise FountainError("stripe out of range")
        self._stripe = stripe
        self._n_stripes = n_stripes
        # serve() is synchronous (no awaits), so concurrent asyncio
        # requests cannot interleave a cursor read/advance — but that
        # invariant is one refactor away from silently vanishing, so
        # the cursor paths take a real lock too (uncontended: ns cost).
        self._serve_lock = threading.Lock()
        self._manifests: Dict[str, dict] = {}
        self._objects: Dict[str, bytes] = {}
        self._packets: Dict[str, Dict[int, bytes]] = {}
        self._order: Dict[str, List[int]] = {}     # sorted packed ids
        self._rotor: Dict[str, int] = {}
        self._pools: Dict[str, _StripePool] = {}

    # ── seeder path ─────────────────────────────────────────────────

    def add_object(self, data: bytes,
                   symbol_size: int = DEFAULT_SYMBOL_SIZE) -> str:
        """Seed a complete object; returns its artifact id."""
        manifest = build_fountain_manifest(data, symbol_size)
        artifact_id = fountain_id(manifest)
        self._manifests[artifact_id] = manifest
        self._objects[artifact_id] = data
        return artifact_id

    # ── fetcher path ────────────────────────────────────────────────

    def add_manifest(self, artifact_id: str, manifest: object) -> dict:
        """Accept a manifest iff it hashes to *artifact_id*."""
        manifest = check_fountain_manifest(manifest)
        if fountain_id(manifest) != artifact_id:
            raise FountainError("manifest does not match artifact id")
        self._manifests.setdefault(artifact_id, manifest)
        self._packets.setdefault(artifact_id, {})
        self._order.setdefault(artifact_id, [])
        return manifest

    def add_packet(self, artifact_id: str, packet: bytes) -> bool:
        """Store a received packet; False = duplicate (id already held)."""
        manifest = self._manifests.get(artifact_id)
        if manifest is None:
            raise FountainError("unknown artifact")
        if len(packet) != packet_length(manifest):
            raise FountainError("packet length does not match symbol size")
        packed = packet_id(packet)
        held = self._packets.setdefault(artifact_id, {})
        if packed in held:
            return False
        held[packed] = packet
        bisect.insort(self._order.setdefault(artifact_id, []), packed)
        return True

    def promote(self, artifact_id: str, data: bytes) -> None:
        """A decoded, *caller-verified* object: this node seeds now."""
        manifest = self._manifests.get(artifact_id)
        if manifest is None:
            raise FountainError("unknown artifact")
        if _sha256_hex(data) != manifest["object"]:
            raise FountainError("object does not match manifest hash")
        self._objects[artifact_id] = data
        self._packets.pop(artifact_id, None)
        self._order.pop(artifact_id, None)
        self._rotor.pop(artifact_id, None)

    # ── reads ───────────────────────────────────────────────────────

    def manifest(self, artifact_id: str) -> Optional[dict]:
        return self._manifests.get(artifact_id)

    def object(self, artifact_id: str) -> Optional[bytes]:
        return self._objects.get(artifact_id)

    def is_complete(self, artifact_id: str) -> bool:
        return artifact_id in self._objects

    def held_ids(self, artifact_id: str) -> List[int]:
        return list(self._order.get(artifact_id, ()))

    def held_ranges(self, artifact_id: str) -> List[List[int]]:
        """Holdings as wire-form exclude ranges (already sorted)."""
        out: List[List[int]] = []
        for i in self._order.get(artifact_id, ()):
            if out and i == out[-1][1]:
                out[-1][1] = i + 1
            else:
                out.append([i, i + 1])
        return out

    def packet(self, artifact_id: str, packed: int) -> Optional[bytes]:
        return self._packets.get(artifact_id, {}).get(packed)

    def drop_packet(self, artifact_id: str, packed: int) -> None:
        held = self._packets.get(artifact_id, {})
        if packed in held:
            del held[packed]
            order = self._order[artifact_id]
            order.pop(bisect.bisect_left(order, packed))

    # ── serving ─────────────────────────────────────────────────────

    def serve(self, artifact_id: str, count: int,
              exclude: Sequence[Tuple[int, int]]) -> List[bytes]:
        """Up to *count* packets the requester does not hold.

        Complete holder: fresh stripe symbols, monotonic. Partial
        holder: stored packets, round-robin from a per-artifact rotor
        so successive requesters sample different regions.
        """
        manifest = self._manifests.get(artifact_id)
        if manifest is None:
            raise FountainError("unknown artifact")
        with self._serve_lock:
            if artifact_id in self._objects:
                pool = self._pools.get(artifact_id)
                if pool is None:
                    pool = self._pools[artifact_id] = _StripePool(
                        self._objects[artifact_id], manifest["symbol_size"],
                        self._stripe, self._n_stripes,
                    )
                return pool.take(count, exclude)

            order = self._order.get(artifact_id, ())
            if not order:
                return []
            start = self._rotor.get(artifact_id, 0) % len(order)
            held = self._packets[artifact_id]
            out: List[bytes] = []
            examined = 0
            for step in range(len(order)):
                if len(out) >= count:
                    break
                packed = order[(start + step) % len(order)]
                if not in_ranges(exclude, packed):
                    out.append(held[packed])
                examined = step + 1
            self._rotor[artifact_id] = (start + examined) % len(order)
            return out


__all__ = [
    "DEFAULT_N_STRIPES",
    "DEFAULT_SYMBOL_SIZE",
    "Decoder",
    "FountainError",
    "FountainStore",
    "build_fountain_manifest",
    "check_fountain_manifest",
    "check_ranges",
    "fountain_id",
    "ids_to_ranges",
    "in_ranges",
    "packet_id",
    "packet_length",
    "roster_stripe",
    "source_symbols",
]
