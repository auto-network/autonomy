"""Attachment object transport: backlog listing, wire framing, drain.

Attachment metadata replicates as ordinary rows; the bytes are
content-addressed and move through this module. The pulling side lists its
quarantined attachment backlog, requests the digests from the serving peer
over the same authenticated channel family as deltas, streams each object
in bounded chunks into a temporary file, verifies size and SHA-256 on
adoption, then re-applies the stored replay identity through ordinary
last-writer-wins and clears the entry. Serving is read-only: candidates come
from the machine's content-addressed store and its local attachments rows.

Wire shape (one request → one framed stream, mirroring the delta channel):

- request: canonical JSON ``{"v", "op": "blob", "digests": [hex64, ...]}``
- per found digest: a ``blob.begin`` JSON frame carrying hash, size,
  filename, and chunk count, then that many ``FSBC``-framed binary chunks
  (magic + u32 sequence + bytes)
- terminal: a ``blob.done`` JSON frame listing found and missing digests
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from tools.network.idkit import canonical_json

from .catalog import MutationCatalog, ensure_quarantine_table
from .codec import decode_mutation_frame
from .compaction import AuthoredMutation
from .materialize import ContentAddressedBlobStore

BLOB_PROTOCOL_VERSION = 1
BLOB_CHUNK_BYTES = 512 * 1024
MAX_BLOB_REQUEST_DIGESTS = 64
_CHUNK_MAGIC = b"FSBC"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class BlobTransportError(ValueError):
    """A blob transfer frame violates the transport contract."""


@dataclass(frozen=True)
class BacklogEntry:
    """One drainable quarantine entry with its replay identity."""

    address: bytes
    digest: str
    size: int
    filename: str
    frame: bytes
    origin: str
    transaction_id: str
    operation_index: int


# -- backlog ----------------------------------------------------------------


def pending_attachment_backlog(conn: sqlite3.Connection) -> list[BacklogEntry]:
    """Quarantined attachment entries that carry a replay identity.

    Checkpoint-quarantined entries store no frame — the next checkpoint
    carries their row again, and locally present bytes realize it then —
    so only delta-deferred entries are drainable here.
    """
    ensure_quarantine_table(conn)
    entries: list[BacklogEntry] = []
    rows = conn.execute(
        "SELECT address,frame,origin,transaction_id,operation_index "
        "FROM fleet_sync_quarantine WHERE reason='attachment_bytes_unavailable' "
        "AND frame IS NOT NULL AND origin IS NOT NULL "
        "AND transaction_id IS NOT NULL AND operation_index IS NOT NULL"
    ).fetchall()
    for address, frame, origin, transaction_id, operation_index in rows:
        mutation = decode_mutation_frame(bytes(frame))
        values = dict(mutation.values)
        digest = str(values.get("hash", ""))
        if not _HEX64.fullmatch(digest):
            continue
        entries.append(BacklogEntry(
            bytes(address), digest, int(values["size_bytes"]),
            str(values.get("filename", "")), bytes(frame),
            str(origin), str(transaction_id), int(operation_index),
        ))
    return entries


# -- request/response framing ----------------------------------------------


def encode_blob_request(digests: list[str]) -> bytes:
    if not digests or len(digests) > MAX_BLOB_REQUEST_DIGESTS:
        raise BlobTransportError("blob request digest count out of bounds")
    for digest in digests:
        if not _HEX64.fullmatch(digest):
            raise BlobTransportError("blob request digest is malformed")
    return canonical_json({
        "v": BLOB_PROTOCOL_VERSION, "op": "blob", "digests": digests,
    })


def decode_blob_request(raw: bytes) -> list[str]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlobTransportError("blob request is not JSON") from exc
    if not isinstance(value, dict) or set(value) != {"v", "op", "digests"}:
        raise BlobTransportError("blob request fields are malformed")
    if value["v"] != BLOB_PROTOCOL_VERSION or value["op"] != "blob":
        raise BlobTransportError("unsupported blob request")
    digests = value["digests"]
    if (
        not isinstance(digests, list)
        or not digests
        or len(digests) > MAX_BLOB_REQUEST_DIGESTS
        or not all(
            isinstance(item, str) and _HEX64.fullmatch(item)
            for item in digests
        )
    ):
        raise BlobTransportError("blob request digests are malformed")
    return list(digests)


def peek_request_op(raw: bytes) -> str | None:
    """The ``op`` field of a JSON request, or None when not a JSON object."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(value, dict) and isinstance(value.get("op"), str):
        return value["op"]
    return None


def _begin_frame(digest: str, size: int, filename: str, chunks: int) -> bytes:
    return canonical_json({
        "v": BLOB_PROTOCOL_VERSION, "kind": "blob.begin",
        "hash": digest, "size": size, "filename": filename, "chunks": chunks,
    })


def _chunk_frame(sequence: int, payload: bytes) -> bytes:
    return _CHUNK_MAGIC + struct.pack(">I", sequence) + payload


def done_frame(found: list[str], missing: list[str]) -> bytes:
    return canonical_json({
        "v": BLOB_PROTOCOL_VERSION, "kind": "blob.done",
        "found": sorted(found), "missing": sorted(missing),
    })


# -- serving ----------------------------------------------------------------


def locate_blob(db_path: Path, digest: str) -> Path | None:
    """A local file whose bytes satisfy the digest, or None.

    Checks the machine's content-addressed store first, then attachment
    rows in the database. Candidates are re-verified by digest before
    serving — a stale file_path never ships wrong bytes.
    """
    db_path = Path(db_path)
    cas_dir = db_path.parent / "uploads" / "fleet" / digest[:2]
    if cas_dir.is_dir():
        for candidate in cas_dir.glob(f"{digest}*"):
            if _digest_of(candidate) == digest:
                return candidate
    if db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for (file_path,) in conn.execute(
                "SELECT file_path FROM attachments WHERE hash=?", (digest,)
            ).fetchall():
                if not file_path:
                    continue
                candidate = Path(str(file_path))
                try:
                    if candidate.is_file() and _digest_of(candidate) == digest:
                        return candidate
                except OSError:
                    continue
        except sqlite3.Error:
            return None
        finally:
            conn.close()
    return None


def _digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(BLOB_CHUNK_BYTES)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def iter_blob_frames(db_path: Path, digests: list[str]) -> Iterator[bytes]:
    """Serve requested digests as a bounded frame stream.

    Reads each file chunk by chunk — memory is bounded by one chunk
    regardless of object size. Digests nothing local satisfies are reported
    in the terminal frame rather than failing the stream.
    """
    found: list[str] = []
    missing: list[str] = []
    for digest in digests:
        path = locate_blob(db_path, digest)
        if path is None:
            missing.append(digest)
            continue
        size = path.stat().st_size
        chunks = max(1, -(-size // BLOB_CHUNK_BYTES))
        yield _begin_frame(digest, size, path.name, chunks)
        with path.open("rb") as handle:
            for sequence in range(chunks):
                yield _chunk_frame(sequence, handle.read(BLOB_CHUNK_BYTES))
        found.append(digest)
    yield done_frame(found, missing)


# -- receiving --------------------------------------------------------------


class BlobReceiver:
    """Feed response frames in order; adopted objects land in the store."""

    def __init__(self, store: ContentAddressedBlobStore) -> None:
        self.store = store
        self.adopted: dict[str, Path] = {}
        self.missing: list[str] = []
        self.done = False
        self._current: dict | None = None
        self._handle = None
        self._temp: Path | None = None
        self._received = 0
        self._sequence = 0

    def feed(self, frame: bytes) -> None:
        if self.done:
            raise BlobTransportError("blob frame after terminal frame")
        if frame.startswith(_CHUNK_MAGIC):
            self._feed_chunk(frame)
            return
        try:
            value = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BlobTransportError("blob control frame is not JSON") from exc
        kind = value.get("kind") if isinstance(value, dict) else None
        if kind == "blob.begin":
            self._begin(value)
        elif kind == "blob.done":
            if self._current is not None:
                raise BlobTransportError("blob stream ended mid-object")
            self.missing = [
                item for item in value.get("missing", ())
                if isinstance(item, str)
            ]
            self.done = True
        else:
            raise BlobTransportError("unknown blob control frame")

    def _begin(self, value: dict) -> None:
        if self._current is not None:
            raise BlobTransportError("blob.begin before prior object ended")
        digest = value.get("hash")
        size = value.get("size")
        chunks = value.get("chunks")
        filename = value.get("filename")
        if (
            not isinstance(digest, str) or not _HEX64.fullmatch(digest)
            or not isinstance(size, int) or size < 0
            or not isinstance(chunks, int) or chunks < 1
            or not isinstance(filename, str)
        ):
            raise BlobTransportError("blob.begin fields are malformed")
        self.store.root.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            dir=self.store.root, prefix=f".recv-{digest[:16]}.", delete=False
        )
        self._current = value
        self._handle = handle
        self._temp = Path(handle.name)
        self._received = 0
        self._sequence = 0

    def _feed_chunk(self, frame: bytes) -> None:
        if self._current is None or self._handle is None:
            raise BlobTransportError("blob chunk outside an object")
        if len(frame) < 8:
            raise BlobTransportError("blob chunk frame is truncated")
        (sequence,) = struct.unpack(">I", frame[4:8])
        if sequence != self._sequence:
            raise BlobTransportError("blob chunk out of order")
        payload = frame[8:]
        if len(payload) > BLOB_CHUNK_BYTES:
            raise BlobTransportError("blob chunk exceeds the chunk bound")
        self._handle.write(payload)
        self._received += len(payload)
        self._sequence += 1
        if self._received > int(self._current["size"]):
            self._abort()
            raise BlobTransportError("blob object exceeds its declared size")
        if self._sequence == int(self._current["chunks"]):
            self._handle.close()
            current, temp = self._current, self._temp
            self._current = self._handle = self._temp = None
            assert temp is not None
            try:
                self.adopted[current["hash"]] = self.store.adopt(
                    current["hash"], int(current["size"]),
                    current["filename"], temp,
                )
            except Exception:
                temp.unlink(missing_ok=True)
                raise

    def _abort(self) -> None:
        if self._handle is not None:
            self._handle.close()
        if self._temp is not None:
            self._temp.unlink(missing_ok=True)
        self._current = self._handle = self._temp = None

    def close(self) -> None:
        self._abort()


# -- draining ---------------------------------------------------------------


def drain_backlog(
    catalog: MutationCatalog, entries: list[BacklogEntry]
) -> int:
    """Re-apply drainable entries through ordinary last-writer-wins.

    The catalog's blob store must be attached; an entry whose bytes it now
    satisfies realizes (or lands as an inert duplicate when a newer winner
    arrived meanwhile) and its quarantine row is cleared. An entry whose
    bytes are still absent re-defers — apply refreshes its quarantine row
    and reports ``(0, 0)`` — and is left in the backlog. Returns the
    cleared count.
    """
    if catalog.blob_store is None:
        raise BlobTransportError("drain requires an attached blob store")
    cleared = 0
    for entry in entries:
        mutation = decode_mutation_frame(entry.frame)
        applied, ignored = catalog.apply_remote_batch([AuthoredMutation(
            entry.origin, entry.transaction_id,
            entry.operation_index, mutation,
        )])
        if not applied and not ignored:
            continue
        with catalog.conn:
            catalog.conn.execute(
                "DELETE FROM fleet_sync_quarantine WHERE address=?",
                (entry.address,),
            )
        cleared += 1
    return cleared
