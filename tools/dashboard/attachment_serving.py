"""On-demand attachment fetch over the share-link channel (auto-1lrip).

Serve one bounded window of an attachment's bytes for a *note* grant,
authorized to the note's content-bound attachment slot set, read from disk in
1 MiB pieces and streamed as channel messages — never loading the whole file.

Wire protocol v1 (frozen): decision note graph://072cd1af-03c, sections
"Channel messages", "Assembly invariants", "Bounds". Integrity is the
channel's per-record AEAD; there is no meta round-trip and no leaf-hash list.

Each yielded body message carries exactly one CHUNK (<= 1 MiB), so the
streaming channel's ~2x-largest-message peak (auto-c31xb memory contract)
stays bounded regardless of file size (D1). Authorization, path containment,
and the byte reads are all blocking work kept off the event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator, Optional

from tools.graph import ops as graph_ops
from tools.network.idkit import canonical_json

# Frozen constants (must match note 072cd1af and attachment_v1.json).
CHUNK = 1024 * 1024               # 1 MiB application chunk
WINDOW = 8 * CHUNK                # 8 MiB max bytes per fetch exchange
MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024 * 1024  # 16 GiB v1 servable cap

_FLAG_LAST_IN_WINDOW = 0x01
_FLAG_EOF = 0x02


def _error(ref: object, code: str, detail: Optional[str] = None) -> bytes:
    """One canonical-JSON error message (bare, no trailing newline)."""
    msg = {
        "v": 1,
        "op": "error",
        "ref": ref if isinstance(ref, str) else "",
        "code": code,
    }
    if detail:
        msg["detail"] = detail
    return canonical_json(msg)


def _body_frame(offset: int, flags: int, chunk: bytes) -> bytes:
    """u64_be(offset) || u8(flags) || chunk_bytes."""
    return offset.to_bytes(8, "big") + bytes([flags]) + chunk


def valid_fetch_request(request: dict) -> bool:
    """True if *request* is a well-formed ``attachment.fetch`` (shape only).

    Semantic refusals (unknown ref, out-of-range offset) are NOT rejected
    here — they are served as error messages so the exchange yields zero body
    bytes. Only protocol misuse (wrong keys/types, non-CHUNK offset/length) is
    a hard bad request.
    """
    keys = set(request)
    if keys not in ({"v", "op", "ref", "offset"},
                    {"v", "op", "ref", "offset", "length"}):
        return False
    ref = request["ref"]
    if not isinstance(ref, str) or not ref:
        return False
    offset = request["offset"]
    # bool is an int subclass; exclude it explicitly.
    if type(offset) is not int or offset < 0 or offset % CHUNK != 0:
        return False
    if "length" in request:
        length = request["length"]
        if type(length) is not int or length <= 0 or length % CHUNK != 0:
            return False
    return True


def valid_cancel_request(request: dict) -> bool:
    """True if *request* is a well-formed ``attachment.cancel``."""
    return (
        set(request) == {"v", "op", "ref"}
        and isinstance(request.get("ref"), str)
        and bool(request.get("ref"))
    )


def _authorize(token: str, ref: str, org: Optional[str], clock):
    """Resolve *ref* to a servable attachment row for the granted note.

    Returns ``(attachment_row, None)`` on success or ``(None, error_code)``.
    Blocking (grant cache + sqlite + filesystem); call via a thread.
    """
    from tools.dashboard import link_serving

    grant = link_serving.check_grant(token, org=org, now=clock())
    if grant is None or grant.get("target_type") != "note":
        # A non-note grant (or none) never serves an attachment.
        return None, "not_authorized"
    note_id = grant.get("target_uuid")
    if not isinstance(note_id, str) or not note_id:
        return None, "not_authorized"

    # Membership is the served note's slot set — NOT the row source_id, and
    # not the requester's choice. A ref outside this set serves nothing.
    slots = graph_ops.note_slot_attachments(note_id, org=org, peers=[])
    att = next((a for a in slots if a.get("id") == ref), None)
    if att is None:
        return None, "not_authorized"

    file_path = att.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None, "not_found"
    # Path containment: the resolved file must live inside the managed
    # attachment store. Anything else (a traversal, a stray absolute path) is
    # refused even though the ref matched a slot row.
    try:
        root = graph_ops.attachment_store_root(org).resolve()
        resolved = Path(file_path).resolve()
    except OSError:
        return None, "not_found"
    if not resolved.is_relative_to(root):
        return None, "not_found"
    if not resolved.is_file():
        return None, "unavailable"
    return {**att, "_resolved_path": str(resolved)}, None


async def fetch_stream(
    token: str, request: dict, *, org: Optional[str] = None, now=None
) -> AsyncIterator[bytes]:
    """Stream one window of an attachment as body frames, or one error message.

    Assumes *request* already passed :func:`valid_fetch_request`.
    """
    import time

    clock = now or time.time
    ref = request["ref"]
    offset = request["offset"]
    requested = request.get("length", WINDOW)

    att, err = await asyncio.to_thread(_authorize, token, ref, org, clock)
    if err is not None:
        yield _error(ref, err)
        return

    total_size = att.get("size_bytes")
    if not isinstance(total_size, int) or total_size < 0:
        yield _error(ref, "unavailable")
        return
    if total_size > MAX_ATTACHMENT_BYTES:
        yield _error(ref, "oversize")
        return

    # A zero-byte attachment: one empty frame that both ends the window and
    # marks EOF (its resulting offset, 0, equals total_size).
    if total_size == 0:
        if offset != 0:
            yield _error(ref, "out_of_range")
            return
        yield _body_frame(0, _FLAG_LAST_IN_WINDOW | _FLAG_EOF, b"")
        return

    if offset >= total_size:
        yield _error(ref, "out_of_range")
        return

    # Window = [offset, effective_end); length is clamped to the window cap and
    # to the file end, so the final chunk may be short.
    effective_end = min(offset + min(requested, WINDOW), total_size)

    path = att["_resolved_path"]
    handle = await asyncio.to_thread(_open_at, path, offset)
    try:
        cursor = offset
        while cursor < effective_end:
            to_read = min(CHUNK, effective_end - cursor)
            chunk = await asyncio.to_thread(handle.read, to_read)
            if len(chunk) != to_read:
                # File shrank/changed under us — refuse rather than serve a
                # short frame the client would misassemble.
                yield _error(ref, "unavailable")
                return
            chunk_end = cursor + len(chunk)
            flags = 0
            if chunk_end == effective_end:
                flags |= _FLAG_LAST_IN_WINDOW
            if chunk_end == total_size:
                flags |= _FLAG_EOF
            yield _body_frame(cursor, flags, chunk)
            cursor = chunk_end
    finally:
        handle.close()


def _open_at(path: str, offset: int):
    """Open *path* and seek to *offset* (blocking; run in a thread)."""
    handle = open(path, "rb")
    try:
        handle.seek(offset)
    except Exception:
        handle.close()
        raise
    return handle
