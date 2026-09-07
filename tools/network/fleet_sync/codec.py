"""Strict canonical bytes for personal-graph replication mutations.

The format is intentionally small and self-describing only at the stream
boundary.  It is not SQLite serialization and it is not JSON: SQLite REAL and
BLOB values need canonical encodings too, while graph JSON columns are decoded
to ordinary dictionaries/lists before reaching this layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import math
import struct
from typing import Any, Iterable, TypeAlias

from .policies import PolicyKind, TABLE_POLICIES


MAGIC = b"autonomy.personal-graph.mutations\x00v1\n"
FRAME_VERSION = 1
MAX_CONTAINER_ITEMS = 1_000_000
MAX_FRAME_BYTES = 64 * 1024 * 1024

CanonicalValue: TypeAlias = (
    None | bool | int | float | str | bytes
    | list["CanonicalValue"] | dict[str, "CanonicalValue"]
)


class CodecError(ValueError):
    """Mutation bytes violate the canonical fleet-sync contract."""


_pack_u32 = struct.Struct(">I").pack
_pack_f64 = struct.Struct(">d").pack


def _u32(value: int) -> bytes:
    if not 0 <= value < 1 << 32:
        raise CodecError("length exceeds u32")
    return _pack_u32(value)


def _blob(tag: bytes, data: bytes) -> bytes:
    return tag + _u32(len(data)) + data


def encode_value(value: CanonicalValue) -> bytes:
    """Encode a typed value into one unique byte representation."""

    # Dispatch is ordered by measured frequency: replication frames are
    # dominated by strings, then ints. The byte layout is frozen; only the
    # construction path is tuned.
    kind = type(value)
    if kind is str:
        data = value.encode("utf-8")
        return b"s" + _pack_u32(len(data)) + data
    if kind is bool:
        return b"t" if value else b"f"
    if kind is int:
        data = str(value).encode("ascii")
        return b"i" + _pack_u32(len(data)) + data
    if value is None:
        return b"n"
    if kind is float:
        if not math.isfinite(value):
            raise CodecError("non-finite floats are not canonical")
        # SQLite compares both signed zeros as zero; preserve only one form.
        if value == 0.0:
            value = 0.0
        return b"r" + _pack_f64(value)
    if kind is bytes:
        return b"b" + _pack_u32(len(value)) + value
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise CodecError("list exceeds item bound")
        return b"l" + _u32(len(value)) + b"".join(
            map(encode_value, value)
        )
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise CodecError("object exceeds item bound")
        try:
            ordered = sorted(
                (key.encode("utf-8"), item) for key, item in value.items()
            )
        except AttributeError:
            raise CodecError("object keys must be strings") from None
        parts = [b"d", _u32(len(ordered))]
        for key_bytes, item in ordered:
            parts.append(b"s" + _pack_u32(len(key_bytes)) + key_bytes)
            parts.append(encode_value(item))
        return b"".join(parts)
    # Fall through for int/str/bytes/float subclasses so the accepted value
    # domain is unchanged; bool subclasses cannot exist.
    if isinstance(value, bool):
        return b"t" if value else b"f"
    if isinstance(value, int):
        return _blob(b"i", str(value).encode("ascii"))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("non-finite floats are not canonical")
        if value == 0.0:
            value = 0.0
        return b"r" + _pack_f64(value)
    if isinstance(value, str):
        return _blob(b"s", value.encode("utf-8"))
    if isinstance(value, bytes):
        return _blob(b"b", value)
    raise CodecError(f"unsupported canonical value: {type(value).__name__}")


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise CodecError("truncated canonical value")
        out = self.data[self.offset:self.offset + size]
        self.offset += size
        return out

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def value(self) -> CanonicalValue:
        tag = self.take(1)
        if tag == b"n":
            return None
        if tag == b"f":
            return False
        if tag == b"t":
            return True
        if tag in {b"i", b"s", b"b"}:
            raw = self.take(self.u32())
            if tag == b"b":
                return raw
            try:
                text = raw.decode("ascii" if tag == b"i" else "utf-8")
            except UnicodeDecodeError as exc:
                raise CodecError("invalid canonical text encoding") from exc
            if tag == b"s":
                return text
            try:
                value = int(text)
            except ValueError as exc:
                raise CodecError("invalid canonical integer") from exc
            if str(value) != text:
                raise CodecError("non-canonical integer spelling")
            return value
        if tag == b"r":
            raw = self.take(8)
            value = struct.unpack(">d", raw)[0]
            if not math.isfinite(value):
                raise CodecError("non-finite floats are not canonical")
            if value == 0.0 and raw != struct.pack(">d", 0.0):
                raise CodecError("negative zero is not canonical")
            return value
        if tag == b"l":
            count = self.u32()
            if count > MAX_CONTAINER_ITEMS:
                raise CodecError("list exceeds item bound")
            return [self.value() for _ in range(count)]
        if tag == b"d":
            count = self.u32()
            if count > MAX_CONTAINER_ITEMS:
                raise CodecError("object exceeds item bound")
            out: dict[str, CanonicalValue] = {}
            previous: bytes | None = None
            for _ in range(count):
                key = self.value()
                if not isinstance(key, str):
                    raise CodecError("object key is not text")
                key_bytes = key.encode("utf-8")
                if previous is not None and key_bytes <= previous:
                    raise CodecError("object keys are not strictly ordered")
                previous = key_bytes
                out[key] = self.value()
            return out
        raise CodecError("unknown canonical value tag")


def decode_value(data: bytes) -> CanonicalValue:
    reader = _Reader(data)
    value = reader.value()
    if reader.offset != len(data):
        raise CodecError("trailing canonical value bytes")
    if encode_value(value) != data:
        raise CodecError("value does not round-trip canonically")
    return value


def _candidate_material(
    table: str,
    address: tuple[CanonicalValue, ...],
    tombstone: bool,
    values: tuple[tuple[str, CanonicalValue], ...],
) -> bytes:
    return encode_value([
        table,
        list(address),
        tombstone,
        {key: value for key, value in values},
    ])


_FRAME_VERSION_BYTES = _blob(b"i", str(FRAME_VERSION).encode("ascii"))
_MATERIAL_HEADER = b"l" + _pack_u32(4)
_FRAME_HEADER = b"l" + _pack_u32(7)


@dataclass(frozen=True)
class Mutation:
    """One addressed graph mutation in the globally deterministic order."""

    table: str
    address: tuple[CanonicalValue, ...]
    timestamp_ns: int
    tombstone: bool
    values: tuple[tuple[str, CanonicalValue], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp_ns, int) or isinstance(self.timestamp_ns, bool):
            raise CodecError("timestamp_ns must be an integer")
        if self.timestamp_ns < 0:
            raise CodecError("timestamp_ns must be non-negative")
        if not self.address:
            raise CodecError("mutation address must not be empty")
        columns = [column for column, _ in self.values]
        if columns != sorted(columns) or len(columns) != len(set(columns)):
            raise CodecError("mutation columns must be unique and sorted")
        if self.tombstone and self.values:
            raise CodecError("tombstones do not carry row values")
        _validate_policy(self)

    @cached_property
    def _encoded(self) -> tuple[bytes, bytes]:
        """One shared encode pass: ``(canonical frame, candidate hash)``.

        The candidate material and the frame share every element except the
        version, timestamp, and trailing hash, so the elements are encoded
        once and assembled into both byte layouts. The layouts themselves
        are frozen — this is construction sharing, not a format change.
        """
        table = encode_value(self.table)
        address = encode_value(list(self.address))
        tombstone = b"t" if self.tombstone else b"f"
        values = encode_value({key: value for key, value in self.values})
        shared = table + address
        digest = hashlib.sha256(
            _MATERIAL_HEADER + shared + tombstone + values
        ).digest()
        frame = (
            _FRAME_HEADER + _FRAME_VERSION_BYTES + shared
            + encode_value(self.timestamp_ns) + tombstone + values
            + b"b" + _pack_u32(32) + digest
        )
        return frame, digest

    @property
    def candidate_hash(self) -> bytes:
        return self._encoded[1]

    @property
    def position(self) -> tuple[int, bytes]:
        return self.timestamp_ns, self.candidate_hash


def _validate_policy(mutation: Mutation) -> None:
    policy = TABLE_POLICIES.get(mutation.table)
    if policy is None:
        raise CodecError(f"unknown logical table: {mutation.table}")
    if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
        raise CodecError(f"table is not replicating: {mutation.table}")
    if mutation.tombstone and policy.kind in {
        PolicyKind.IMMUTABLE, PolicyKind.IMMUTABLE_PRUNABLE,
    }:
        raise CodecError(f"immutable table does not accept tombstones: {mutation.table}")
    width = len(policy.key)
    if mutation.table == "settings" and len(mutation.address) == width + 1:
        # One slot per signer (graph://21a0da9e-1c2): a signed settings
        # row's address ends with its terminal persona. Unsigned rows keep
        # the five-part address, so the policy inventory (and with it the
        # compatibility digest) is unchanged.
        persona = mutation.address[width]
        if not isinstance(persona, str) or len(persona) != 64 \
                or any(ch not in "0123456789abcdef" for ch in persona):
            raise CodecError("settings signer address must be a 64-hex persona")
    elif len(mutation.address) != width:
        raise CodecError(f"wrong logical address width for {mutation.table}")
    value_columns = {column for column, _ in mutation.values}
    forbidden = value_columns.intersection(policy.excluded_columns)
    if forbidden:
        raise CodecError(
            f"machine-local columns in {mutation.table}: {', '.join(sorted(forbidden))}"
        )
def _frame(mutation: Mutation) -> bytes:
    return mutation._encoded[0]


def encode_mutation_frame(mutation: Mutation) -> bytes:
    """Return one canonical frame without constructing a whole stream."""
    frame = _frame(mutation)
    if len(frame) > MAX_FRAME_BYTES:
        raise CodecError("mutation frame exceeds size bound")
    return frame


def _sort_key(mutation: Mutation) -> tuple[int, bytes, bytes]:
    frame = _frame(mutation)
    return mutation.timestamp_ns, mutation.candidate_hash, frame


def encode_stream(mutations: Iterable[Mutation]) -> bytes:
    # Replay is semantically inert and must also be byte-inert.  Keying by the
    # complete canonical frame avoids relying on Python hashability of nested
    # JSON values.
    unique = {_frame(mutation): mutation for mutation in mutations}
    ordered = sorted(unique.values(), key=_sort_key)
    frames = [_frame(mutation) for mutation in ordered]
    if any(len(frame) > MAX_FRAME_BYTES for frame in frames):
        raise CodecError("mutation frame exceeds size bound")
    return MAGIC + _u32(len(frames)) + b"".join(
        _u32(len(frame)) + frame for frame in frames
    )


def _mutation_from_frame(frame: bytes) -> Mutation:
    value = decode_value(frame)
    if not isinstance(value, list) or len(value) != 7:
        raise CodecError("mutation frame has wrong shape")
    version, table, address, timestamp_ns, tombstone, raw_values, claimed_hash = value
    if version != FRAME_VERSION:
        raise CodecError("unsupported mutation frame version")
    if not isinstance(table, str):
        raise CodecError("mutation table must be text")
    if not isinstance(address, list):
        raise CodecError("mutation address must be a list")
    if not isinstance(tombstone, bool):
        raise CodecError("mutation tombstone flag must be boolean")
    if not isinstance(raw_values, dict):
        raise CodecError("mutation values must be an object")
    if not isinstance(claimed_hash, bytes) or len(claimed_hash) != 32:
        raise CodecError("mutation candidate hash has wrong shape")
    mutation = Mutation(
        table=table,
        address=tuple(address),
        timestamp_ns=timestamp_ns,
        tombstone=tombstone,
        values=tuple(raw_values.items()),
    )
    if mutation.candidate_hash != claimed_hash:
        raise CodecError("mutation candidate hash mismatch")
    if _frame(mutation) != frame:
        raise CodecError("mutation frame is not canonical")
    return mutation


def decode_mutation_frame(frame: bytes) -> Mutation:
    """Strictly decode one frame emitted by :func:`encode_mutation_frame`."""
    if len(frame) > MAX_FRAME_BYTES:
        raise CodecError("mutation frame exceeds size bound")
    return _mutation_from_frame(frame)


def decode_stream(data: bytes) -> list[Mutation]:
    if not data.startswith(MAGIC):
        raise CodecError("unsupported mutation stream domain/version")
    reader = _Reader(data[len(MAGIC):])
    count = reader.u32()
    if count > MAX_CONTAINER_ITEMS:
        raise CodecError("mutation stream exceeds item bound")
    mutations: list[Mutation] = []
    for _ in range(count):
        length = reader.u32()
        if length > MAX_FRAME_BYTES:
            raise CodecError("mutation frame exceeds size bound")
        mutations.append(_mutation_from_frame(reader.take(length)))
    if reader.offset != len(reader.data):
        raise CodecError("trailing mutation stream bytes")
    keys = [_sort_key(mutation) for mutation in mutations]
    if keys != sorted(keys) or any(
        left >= right for left, right in zip(keys, keys[1:])
    ):
        raise CodecError("mutation frames are not strictly ordered")
    if encode_stream(mutations) != data:
        raise CodecError("mutation stream is not canonical")
    return mutations
