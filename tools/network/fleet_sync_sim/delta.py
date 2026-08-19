"""Strict bounded-memory delta artifacts for the fleet-sync alpha."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
from typing import Iterable, Iterator

from .catalog import (
    MAX_TRANSACTION_FRAME_BYTES, MAX_TRANSACTION_OPERATIONS, MutationCatalog,
)
from .codec import (
    CodecError,
    MAX_FRAME_BYTES,
    decode_mutation_frame,
    decode_value,
    encode_mutation_frame,
    encode_value,
)
from .compaction import AuthoredMutation
from .streaming import policy_digest


DELTA_CHUNK_MAGIC = b"AUTONOMY-PERSONAL-DELTA-CHUNK\x00v1\n"
DELTA_FRAME_VERSION = 1
DELTA_CATALOG_VERSION = 1
MAX_DELTA_FRAME_BYTES = MAX_FRAME_BYTES + 1024
_U32 = struct.Struct(">I")


class DeltaCodecError(ValueError):
    pass


def encode_authored_frame(item: AuthoredMutation) -> bytes:
    frame = encode_value([
        DELTA_FRAME_VERSION,
        item.origin_incarnation,
        item.transaction_id,
        item.operation_index,
        encode_mutation_frame(item.mutation),
    ])
    if len(frame) > MAX_DELTA_FRAME_BYTES:
        raise DeltaCodecError("authored mutation frame exceeds size bound")
    return frame


def decode_authored_frame(frame: bytes) -> AuthoredMutation:
    if len(frame) > MAX_DELTA_FRAME_BYTES:
        raise DeltaCodecError("authored mutation frame exceeds size bound")
    try:
        value = decode_value(frame)
    except CodecError as exc:
        raise DeltaCodecError(str(exc)) from exc
    if not isinstance(value, list) or len(value) != 5:
        raise DeltaCodecError("authored mutation frame has wrong shape")
    version, origin, transaction, operation, mutation_frame = value
    if version != DELTA_FRAME_VERSION:
        raise DeltaCodecError("unsupported authored mutation frame")
    if not isinstance(origin, str) or not origin:
        raise DeltaCodecError("origin incarnation must be non-empty text")
    if not isinstance(transaction, str) or not transaction:
        raise DeltaCodecError("transaction id must be non-empty text")
    if not isinstance(operation, int) or isinstance(operation, bool) or operation < 0:
        raise DeltaCodecError("operation index must be non-negative integer")
    if not isinstance(mutation_frame, bytes):
        raise DeltaCodecError("logical mutation must be encoded bytes")
    item = AuthoredMutation(
        origin, transaction, operation, decode_mutation_frame(mutation_frame)
    )
    if encode_authored_frame(item) != frame:
        raise DeltaCodecError("authored mutation frame is not canonical")
    return item


def authored_order(item: AuthoredMutation) -> tuple[object, ...]:
    mutation = item.mutation
    address = encode_value([mutation.table, list(mutation.address)])
    return (
        mutation.timestamp_ns, item.origin_incarnation,
        item.transaction_id, item.operation_index,
        mutation.table, address, mutation.candidate_hash,
    )


@dataclass(frozen=True)
class DeltaChunkEntry:
    sequence: int
    filename: str
    records: int
    bytes: int
    sha256: str
    first_position: str
    last_position: str


@dataclass(frozen=True)
class DeltaCatalog:
    version: int
    policy_digest: str
    through_watermark: int
    target_chunk_bytes: int
    total_records: int
    total_bytes: int
    root_sha256: str
    chunks: tuple[DeltaChunkEntry, ...]


def read_delta_catalog(directory: Path) -> DeltaCatalog:
    body = (directory / "delta-catalog.json").read_bytes()
    value = json.loads(body)
    if not isinstance(value, dict):
        raise DeltaCodecError("delta catalog has wrong shape")
    if json.dumps(value, sort_keys=True, separators=(",", ":")).encode() != body:
        raise DeltaCodecError("delta catalog is not canonical JSON")
    try:
        integers = (
            "version", "through_watermark", "target_chunk_bytes",
            "total_records", "total_bytes",
        )
        if any(
            not isinstance(value.get(name), int)
            or isinstance(value.get(name), bool)
            or value[name] < 0
            for name in integers
        ):
            raise DeltaCodecError("delta catalog integer has wrong type")
        if not isinstance(value.get("policy_digest"), str) or not isinstance(
            value.get("root_sha256"), str
        ):
            raise DeltaCodecError("delta catalog digest has wrong type")
        return DeltaCatalog(
            version=value["version"],
            policy_digest=value["policy_digest"],
            through_watermark=value["through_watermark"],
            target_chunk_bytes=value["target_chunk_bytes"],
            total_records=value["total_records"],
            total_bytes=value["total_bytes"],
            root_sha256=value["root_sha256"],
            chunks=tuple(DeltaChunkEntry(**entry) for entry in value["chunks"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DeltaCodecError("delta catalog has wrong shape") from exc


class _Writer:
    def __init__(self, directory: Path, sequence: int, digest_hex: str) -> None:
        self.directory = directory
        self.sequence = sequence
        self.temporary = directory / f".{sequence:08d}.delta.tmp"
        self.handle = self.temporary.open("w+b")
        self.handle.write(DELTA_CHUNK_MAGIC)
        self.handle.write(bytes.fromhex(digest_hex))
        self.handle.write(_U32.pack(sequence))
        self.count_offset = self.handle.tell()
        self.handle.write(_U32.pack(0))
        self.records = 0
        self.first_position = ""
        self.last_position = ""

    @property
    def size(self) -> int:
        return self.handle.tell()

    def append(self, item: AuthoredMutation, frame: bytes) -> None:
        position = encode_value([
            item.mutation.timestamp_ns, item.mutation.table,
            list(item.mutation.address), item.mutation.candidate_hash,
        ]).hex()
        if not self.first_position:
            self.first_position = position
        self.last_position = position
        self.handle.write(_U32.pack(len(frame)))
        self.handle.write(frame)
        self.records += 1

    def finish(self) -> DeltaChunkEntry:
        self.handle.seek(self.count_offset)
        self.handle.write(_U32.pack(self.records))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        digest = hashlib.sha256()
        size = 0
        with self.temporary.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        target = self.directory / f"{self.sequence:08d}-{digest.hexdigest()}.delta"
        self.temporary.replace(target)
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return DeltaChunkEntry(
            self.sequence, target.name, self.records, size, digest.hexdigest(),
            self.first_position, self.last_position,
        )


def stream_delta_to_chunks(
    items: Iterable[AuthoredMutation],
    directory: Path,
    *,
    through_watermark: int,
    target_chunk_bytes: int = 4 * 1024 * 1024,
) -> DeltaCatalog:
    if target_chunk_bytes < 4096:
        raise ValueError("target_chunk_bytes is too small")
    directory.mkdir(parents=True, exist_ok=True)
    digest_hex = policy_digest()
    entries: list[DeltaChunkEntry] = []
    writer = _Writer(directory, 0, digest_hex)
    previous: tuple[object, ...] | None = None
    total = 0
    try:
        for item in items:
            order = authored_order(item)
            if previous is not None and order <= previous:
                raise DeltaCodecError("delta input is not in strict canonical order")
            if item.mutation.timestamp_ns > through_watermark:
                raise DeltaCodecError("delta exceeds declared watermark")
            frame = encode_authored_frame(item)
            if writer.records and writer.size + 4 + len(frame) > target_chunk_bytes:
                entries.append(writer.finish())
                writer = _Writer(directory, len(entries), digest_hex)
            writer.append(item, frame)
            previous = order
            total += 1
        if writer.records:
            entries.append(writer.finish())
        else:
            writer.handle.close()
            writer.temporary.unlink(missing_ok=True)
    except Exception:
        if not writer.handle.closed:
            writer.handle.close()
        writer.temporary.unlink(missing_ok=True)
        raise
    root = hashlib.sha256(json.dumps(
        [entry.sha256 for entry in entries], separators=(",", ":")
    ).encode()).hexdigest()
    catalog = DeltaCatalog(
        DELTA_CATALOG_VERSION, digest_hex, through_watermark,
        target_chunk_bytes, total, sum(entry.bytes for entry in entries),
        root, tuple(entries),
    )
    body = json.dumps({
        **asdict(catalog), "chunks": [asdict(entry) for entry in entries],
    }, sort_keys=True, separators=(",", ":")).encode()
    temporary = directory / ".delta-catalog.tmp"
    with temporary.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(directory / "delta-catalog.json")
    return catalog


def iter_delta_chunk(path: Path, expected_sequence: int) -> Iterator[AuthoredMutation]:
    with path.open("rb") as handle:
        if handle.read(len(DELTA_CHUNK_MAGIC)) != DELTA_CHUNK_MAGIC:
            raise DeltaCodecError("unsupported delta chunk")
        if handle.read(32).hex() != policy_digest():
            raise DeltaCodecError("delta policy mismatch")
        raw_sequence = handle.read(4)
        raw_count = handle.read(4)
        if len(raw_sequence) != 4 or len(raw_count) != 4:
            raise DeltaCodecError("truncated delta header")
        if _U32.unpack(raw_sequence)[0] != expected_sequence:
            raise DeltaCodecError("delta sequence mismatch")
        count = _U32.unpack(raw_count)[0]
        for _ in range(count):
            raw_length = handle.read(4)
            if len(raw_length) != 4:
                raise DeltaCodecError("truncated delta frame length")
            length = _U32.unpack(raw_length)[0]
            if length > MAX_DELTA_FRAME_BYTES:
                raise DeltaCodecError("delta frame exceeds size bound")
            frame = handle.read(length)
            if len(frame) != length:
                raise DeltaCodecError("truncated delta frame")
            yield decode_authored_frame(frame)
        if handle.read(1):
            raise DeltaCodecError("trailing delta bytes")


def iter_delta_catalog(
    directory: Path, catalog: DeltaCatalog
) -> Iterator[AuthoredMutation]:
    if catalog.policy_digest != policy_digest():
        raise DeltaCodecError("delta catalog policy mismatch")
    if [entry.sequence for entry in catalog.chunks] != list(range(len(catalog.chunks))):
        raise DeltaCodecError("delta catalog sequence is not contiguous")
    root = hashlib.sha256(json.dumps(
        [entry.sha256 for entry in catalog.chunks], separators=(",", ":")
    ).encode()).hexdigest()
    if root != catalog.root_sha256:
        raise DeltaCodecError("delta catalog root mismatch")
    previous: tuple[object, ...] | None = None
    for entry in catalog.chunks:
        if entry.filename != f"{entry.sequence:08d}-{entry.sha256}.delta":
            raise DeltaCodecError("delta artifact filename is not canonical")
        path = directory / entry.filename
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != entry.sha256:
            raise DeltaCodecError("delta chunk digest mismatch")
        for item in iter_delta_chunk(path, entry.sequence):
            order = authored_order(item)
            if previous is not None and order <= previous:
                raise DeltaCodecError("delta records are not strictly ordered")
            if item.mutation.timestamp_ns > catalog.through_watermark:
                raise DeltaCodecError("delta record exceeds watermark")
            previous = order
            yield item


def apply_delta_catalog(
    target: MutationCatalog, directory: Path, catalog: DeltaCatalog
) -> tuple[int, int]:
    applied = ignored = 0
    group: list[AuthoredMutation] = []
    group_identity: tuple[str, str] | None = None
    group_bytes = 0
    for item in iter_delta_catalog(directory, catalog):
        identity = (item.origin_incarnation, item.transaction_id)
        if group and identity != group_identity:
            group_applied, group_ignored = target.apply_remote_batch(group)
            applied += group_applied
            ignored += group_ignored
            group.clear()
            group_bytes = 0
        group_identity = identity
        if len(group) >= MAX_TRANSACTION_OPERATIONS:
            raise DeltaCodecError("delta transaction exceeds operation bound")
        group_bytes += len(encode_authored_frame(item))
        if group_bytes > MAX_TRANSACTION_FRAME_BYTES:
            raise DeltaCodecError("delta transaction exceeds byte bound")
        group.append(item)
    if group:
        group_applied, group_ignored = target.apply_remote_batch(group)
        applied += group_applied
        ignored += group_ignored
    return applied, ignored
