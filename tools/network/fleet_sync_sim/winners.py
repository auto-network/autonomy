"""Strict payload-free winner/tombstone artifacts for exact alpha bases."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
from typing import Iterable, Iterator

from .catalog import MutationCatalog, WinnerMetadata
from .codec import CodecError, decode_value, encode_value
from .streaming import policy_digest


WINNER_CHUNK_MAGIC = b"AUTONOMY-PERSONAL-WINNER-CHUNK\x00v1\n"
WINNER_FRAME_VERSION = 1
WINNER_CATALOG_VERSION = 1
MAX_WINNER_FRAME_BYTES = 64 * 1024
_U32 = struct.Struct(">I")


class WinnerCodecError(ValueError):
    pass


def encode_winner_frame(item: WinnerMetadata) -> bytes:
    frame = encode_value([
        WINNER_FRAME_VERSION, item.origin_incarnation, item.transaction_id,
        item.operation_index, item.table, list(item.address), item.timestamp_ns,
        item.tombstone, item.candidate_hash,
    ])
    if len(frame) > MAX_WINNER_FRAME_BYTES:
        raise WinnerCodecError("winner frame exceeds size bound")
    return frame


def decode_winner_frame(frame: bytes) -> WinnerMetadata:
    if len(frame) > MAX_WINNER_FRAME_BYTES:
        raise WinnerCodecError("winner frame exceeds size bound")
    try:
        value = decode_value(frame)
    except CodecError as exc:
        raise WinnerCodecError(str(exc)) from exc
    if not isinstance(value, list) or len(value) != 9:
        raise WinnerCodecError("winner frame has wrong shape")
    version, origin, transaction, operation, table, address, timestamp, tombstone, digest = value
    if version != WINNER_FRAME_VERSION:
        raise WinnerCodecError("unsupported winner frame")
    if not isinstance(origin, str) or not origin:
        raise WinnerCodecError("winner origin must be non-empty text")
    if not isinstance(transaction, str) or not transaction:
        raise WinnerCodecError("winner transaction must be non-empty text")
    if not isinstance(operation, int) or isinstance(operation, bool) or operation < 0:
        raise WinnerCodecError("winner operation must be non-negative integer")
    if not isinstance(table, str) or not table or not isinstance(address, list):
        raise WinnerCodecError("winner address has wrong shape")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        raise WinnerCodecError("winner timestamp must be non-negative integer")
    if not isinstance(tombstone, bool):
        raise WinnerCodecError("winner tombstone must be boolean")
    if not isinstance(digest, bytes) or len(digest) != 32:
        raise WinnerCodecError("winner candidate hash must be 32 bytes")
    item = WinnerMetadata(
        origin, transaction, operation, table, tuple(address), timestamp,
        tombstone, digest,
    )
    if encode_winner_frame(item) != frame:
        raise WinnerCodecError("winner frame is not canonical")
    return item


def winner_order(item: WinnerMetadata) -> tuple[object, ...]:
    return (
        item.timestamp_ns, item.origin_incarnation, item.transaction_id,
        item.operation_index, item.table,
        encode_value([item.table, list(item.address)]), item.candidate_hash,
    )


@dataclass(frozen=True)
class WinnerChunkEntry:
    sequence: int
    filename: str
    records: int
    bytes: int
    sha256: str


@dataclass(frozen=True)
class WinnerCatalog:
    version: int
    policy_digest: str
    through_watermark: int
    target_chunk_bytes: int
    total_records: int
    total_bytes: int
    root_sha256: str
    chunks: tuple[WinnerChunkEntry, ...]


class _Writer:
    def __init__(self, directory: Path, sequence: int, digest_hex: str) -> None:
        self.directory = directory
        self.sequence = sequence
        self.temporary = directory / f".{sequence:08d}.winner.tmp"
        self.handle = self.temporary.open("w+b")
        self.handle.write(WINNER_CHUNK_MAGIC)
        self.handle.write(bytes.fromhex(digest_hex))
        self.handle.write(_U32.pack(sequence))
        self.count_offset = self.handle.tell()
        self.handle.write(_U32.pack(0))
        self.records = 0

    @property
    def size(self) -> int:
        return self.handle.tell()

    def append(self, frame: bytes) -> None:
        self.handle.write(_U32.pack(len(frame)))
        self.handle.write(frame)
        self.records += 1

    def finish(self) -> WinnerChunkEntry:
        self.handle.seek(self.count_offset)
        self.handle.write(_U32.pack(self.records))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        body = self.temporary.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        target = self.directory / f"{self.sequence:08d}-{digest}.winner"
        self.temporary.replace(target)
        return WinnerChunkEntry(
            self.sequence, target.name, self.records, len(body), digest
        )


def stream_winners_to_chunks(
    items: Iterable[WinnerMetadata], directory: Path, *, through_watermark: int,
    target_chunk_bytes: int = 4 * 1024 * 1024,
) -> WinnerCatalog:
    if target_chunk_bytes < 4096:
        raise ValueError("target_chunk_bytes is too small")
    directory.mkdir(parents=True, exist_ok=True)
    digest_hex = policy_digest()
    entries: list[WinnerChunkEntry] = []
    writer = _Writer(directory, 0, digest_hex)
    previous: tuple[object, ...] | None = None
    total = 0
    try:
        for item in items:
            order = winner_order(item)
            if previous is not None and order <= previous:
                raise WinnerCodecError("winner input is not in strict order")
            if item.timestamp_ns > through_watermark:
                raise WinnerCodecError("winner exceeds declared watermark")
            frame = encode_winner_frame(item)
            if writer.records and writer.size + 4 + len(frame) > target_chunk_bytes:
                entries.append(writer.finish())
                writer = _Writer(directory, len(entries), digest_hex)
            writer.append(frame)
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
    catalog = WinnerCatalog(
        WINNER_CATALOG_VERSION, digest_hex, through_watermark,
        target_chunk_bytes, total, sum(entry.bytes for entry in entries),
        root, tuple(entries),
    )
    body = json.dumps({
        **asdict(catalog), "chunks": [asdict(entry) for entry in entries],
    }, sort_keys=True, separators=(",", ":")).encode()
    temporary = directory / ".winner-catalog.tmp"
    with temporary.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(directory / "winner-catalog.json")
    return catalog


def iter_winner_chunk(path: Path, expected_sequence: int) -> Iterator[WinnerMetadata]:
    with path.open("rb") as handle:
        if handle.read(len(WINNER_CHUNK_MAGIC)) != WINNER_CHUNK_MAGIC:
            raise WinnerCodecError("unsupported winner chunk")
        if handle.read(32).hex() != policy_digest():
            raise WinnerCodecError("winner policy mismatch")
        sequence = handle.read(4)
        count = handle.read(4)
        if len(sequence) != 4 or len(count) != 4:
            raise WinnerCodecError("truncated winner header")
        if _U32.unpack(sequence)[0] != expected_sequence:
            raise WinnerCodecError("winner sequence mismatch")
        for _ in range(_U32.unpack(count)[0]):
            length = handle.read(4)
            if len(length) != 4:
                raise WinnerCodecError("truncated winner frame length")
            size = _U32.unpack(length)[0]
            if size > MAX_WINNER_FRAME_BYTES:
                raise WinnerCodecError("winner frame exceeds size bound")
            frame = handle.read(size)
            if len(frame) != size:
                raise WinnerCodecError("truncated winner frame")
            yield decode_winner_frame(frame)
        if handle.read(1):
            raise WinnerCodecError("trailing winner bytes")


def iter_winner_catalog(
    directory: Path, catalog: WinnerCatalog
) -> Iterator[WinnerMetadata]:
    if catalog.policy_digest != policy_digest():
        raise WinnerCodecError("winner catalog policy mismatch")
    if [entry.sequence for entry in catalog.chunks] != list(range(len(catalog.chunks))):
        raise WinnerCodecError("winner catalog sequence is not contiguous")
    root = hashlib.sha256(json.dumps(
        [entry.sha256 for entry in catalog.chunks], separators=(",", ":")
    ).encode()).hexdigest()
    if root != catalog.root_sha256:
        raise WinnerCodecError("winner catalog root mismatch")
    previous: tuple[object, ...] | None = None
    for entry in catalog.chunks:
        if entry.filename != f"{entry.sequence:08d}-{entry.sha256}.winner":
            raise WinnerCodecError("winner artifact filename is not canonical")
        path = directory / entry.filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.sha256:
            raise WinnerCodecError("winner chunk digest mismatch")
        for item in iter_winner_chunk(path, entry.sequence):
            order = winner_order(item)
            if previous is not None and order <= previous:
                raise WinnerCodecError("winner records are not strictly ordered")
            if item.timestamp_ns > catalog.through_watermark:
                raise WinnerCodecError("winner exceeds declared watermark")
            previous = order
            yield item


def install_winner_catalog(
    target: MutationCatalog, directory: Path, catalog: WinnerCatalog
) -> int:
    return target.install_winner_metadata(iter_winner_catalog(directory, catalog))
