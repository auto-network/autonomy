"""Strict canonical bytes for personal-graph replication mutations.

The format is intentionally small and self-describing only at the stream
boundary.  It is not SQLite serialization and it is not JSON: SQLite REAL and
BLOB values need canonical encodings too, while graph JSON columns are decoded
to ordinary dictionaries/lists before reaching this layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct
from typing import Any, Iterable, TypeAlias

from .policies import (
    EXCLUDED_SETTING_SET_IDS,
    PolicyKind,
    TABLE_POLICIES,
)


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


def _u32(value: int) -> bytes:
    if not 0 <= value < 1 << 32:
        raise CodecError("length exceeds u32")
    return struct.pack(">I", value)


def _blob(tag: bytes, data: bytes) -> bytes:
    return tag + _u32(len(data)) + data


def encode_value(value: CanonicalValue) -> bytes:
    """Encode a typed value into one unique byte representation."""

    if value is None:
        return b"n"
    if value is False:
        return b"f"
    if value is True:
        return b"t"
    if isinstance(value, int):
        return _blob(b"i", str(value).encode("ascii"))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("non-finite floats are not canonical")
        # SQLite compares both signed zeros as zero; preserve only one form.
        if value == 0.0:
            value = 0.0
        return b"r" + struct.pack(">d", value)
    if isinstance(value, str):
        return _blob(b"s", value.encode("utf-8"))
    if isinstance(value, bytes):
        return _blob(b"b", value)
    if isinstance(value, list):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise CodecError("list exceeds item bound")
        return b"l" + _u32(len(value)) + b"".join(
            encode_value(item) for item in value
        )
    if isinstance(value, dict):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise CodecError("object exceeds item bound")
        if not all(isinstance(key, str) for key in value):
            raise CodecError("object keys must be strings")
        ordered = sorted(value.items(), key=lambda item: item[0].encode("utf-8"))
        return b"d" + _u32(len(ordered)) + b"".join(
            encode_value(key) + encode_value(item) for key, item in ordered
        )
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

    @property
    def candidate_hash(self) -> bytes:
        return hashlib.sha256(_candidate_material(
            self.table, self.address, self.tombstone, self.values
        )).digest()

    @property
    def position(self) -> tuple[int, bytes]:
        return self.timestamp_ns, self.candidate_hash


def _validate_policy(mutation: Mutation) -> None:
    policy = TABLE_POLICIES.get(mutation.table)
    if policy is None:
        raise CodecError(f"unknown logical table: {mutation.table}")
    if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
        raise CodecError(f"table is not replicating: {mutation.table}")
    if len(mutation.address) != len(policy.key):
        raise CodecError(f"wrong logical address width for {mutation.table}")
    value_columns = {column for column, _ in mutation.values}
    forbidden = value_columns.intersection(policy.excluded_columns)
    if forbidden:
        raise CodecError(
            f"machine-local columns in {mutation.table}: {', '.join(sorted(forbidden))}"
        )
    if mutation.table == "settings":
        set_id = mutation.address[0]
        if set_id in EXCLUDED_SETTING_SET_IDS:
            raise CodecError(f"excluded identity Setting: {set_id}")


def _frame(mutation: Mutation) -> bytes:
    return encode_value([
        FRAME_VERSION,
        mutation.table,
        list(mutation.address),
        mutation.timestamp_ns,
        mutation.tombstone,
        {key: value for key, value in mutation.values},
        mutation.candidate_hash,
    ])


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
