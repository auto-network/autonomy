"""Headless attachment window/resume reference driver (auto-ed336).

This module owns the client-side state machine shared conceptually by the
browser and Go implementations. It intentionally knows nothing about OPFS,
WebSockets, or Go files: callers provide a bounded message stream for each
``attachment.fetch`` request, a random-access durable sink, and a cursor store.

The server keeps no resume state. Safety comes from the authenticated manifest
identity, strict body-frame assembly, durable-write-before-cursor ordering, and
requesting exactly one 8 MiB window at a time.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from tools.network.idkit import canonical_json

CHUNK = 1024 * 1024
WINDOW = 8 * CHUNK
LAST_IN_WINDOW = 0x01
EOF = 0x02
_KNOWN_BODY_FLAGS = LAST_IN_WINDOW | EOF
_CURSOR_DOMAIN = b"autonomy.attachment.cursor.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_CODES = frozenset({
    "not_found",
    "not_authorized",
    "oversize",
    "out_of_range",
    "unavailable",
    "internal",
})
_CURSOR_FIELDS = frozenset({
    "v",
    "cursor_id",
    "note_id",
    "ref",
    "raw_sha256",
    "total_size",
    "committed_offset",
    "sink_id",
})


class AttachmentDownloadError(Exception):
    """Base class for a refused, interrupted, or malformed transfer."""


class AttachmentDisconnected(AttachmentDownloadError):
    """The current channel ended before its window completed."""


class AttachmentAssemblyError(AttachmentDownloadError):
    """Authenticated application messages violated the assembly contract."""


class AttachmentRefused(AttachmentDownloadError):
    """The server ended an exchange with a typed attachment error."""

    def __init__(self, code: str):
        super().__init__(f"attachment fetch refused: {code}")
        self.code = code


class AttachmentAlreadyActive(AttachmentDownloadError):
    """A second writer attempted the same token/ref transfer."""


@dataclass(frozen=True)
class AttachmentIdentity:
    ref: str
    raw_sha256: str
    total_size: int
    oversize: bool = False

    @classmethod
    def from_manifest(cls, entry: dict) -> "AttachmentIdentity":
        if not isinstance(entry, dict):
            raise ValueError("attachment manifest entry must be an object")
        ref = entry.get("ref")
        raw_sha256 = entry.get("raw_sha256")
        total_size = entry.get("total_size")
        oversize = entry.get("oversize", False)
        if not isinstance(ref, str) or not ref:
            raise ValueError("attachment ref must be a non-empty string")
        if not (
            isinstance(raw_sha256, str)
            and _SHA256_RE.fullmatch(raw_sha256)
        ):
            raise ValueError("raw_sha256 must be 64 lowercase hex characters")
        if type(total_size) is not int or total_size < 0:
            raise ValueError("total_size must be a nonnegative integer")
        if not isinstance(oversize, bool):
            raise ValueError("oversize must be boolean")
        return cls(ref, raw_sha256, total_size, oversize)


@dataclass(frozen=True)
class DownloadResult:
    status: str
    committed_offset: int
    windows_completed: int


class MemoryCursorStore:
    """In-memory cursor persistence and single-writer lock for headless tests."""

    def __init__(self):
        self.records: dict[str, dict] = {}
        self._active: set[str] = set()

    def get(self, key: str) -> dict | None:
        value = self.records.get(key)
        return dict(value) if isinstance(value, dict) else value

    def put(self, key: str, value: dict) -> None:
        self.records[key] = dict(value)

    def delete(self, key: str) -> None:
        self.records.pop(key, None)

    def acquire(self, key: str) -> bool:
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def release(self, key: str) -> None:
        self._active.discard(key)


class MemoryAttachmentSink:
    """Random-access durable-sink stand-in used by the reference driver."""

    def __init__(self, sink_id: str = "memory"):
        if not isinstance(sink_id, str) or not sink_id:
            raise ValueError("sink_id must be a non-empty string")
        self.sink_id = sink_id
        self._data = bytearray()
        self.flush_count = 0

    async def write_at(self, offset: int, data: bytes) -> None:
        if offset < 0 or offset > len(self._data):
            raise OSError("non-contiguous sink write")
        end = offset + len(data)
        if end > len(self._data):
            self._data.extend(b"\x00" * (end - len(self._data)))
        self._data[offset:end] = data

    async def flush(self) -> None:
        self.flush_count += 1

    async def truncate(self, size: int) -> None:
        if size < 0:
            raise OSError("negative truncate")
        del self._data[size:]
        if len(self._data) < size:
            self._data.extend(b"\x00" * (size - len(self._data)))

    @property
    def data(self) -> bytes:
        return bytes(self._data)

    @property
    def size(self) -> int:
        return len(self._data)


def cursor_id(link_token: str) -> str:
    """Derive a non-reversible cursor namespace without persisting the token."""
    if not isinstance(link_token, str) or not link_token:
        raise ValueError("link_token must be a non-empty string")
    return hmac.new(
        _CURSOR_DOMAIN, link_token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class AttachmentDownloader:
    """Pull, validate, persist, pause, and resume one attachment."""

    def __init__(
        self,
        manifest_entry: dict,
        *,
        link_token: str,
        note_id: str,
        sink,
        cursors: MemoryCursorStore,
        fetch_window: Callable[
            [dict],
            AsyncIterator[bytes] | Awaitable[AsyncIterator[bytes]],
        ],
    ):
        self.identity = AttachmentIdentity.from_manifest(manifest_entry)
        if not isinstance(note_id, str) or not note_id:
            raise ValueError("note_id must be a non-empty string")
        if not (
            isinstance(getattr(sink, "sink_id", None), str)
            and sink.sink_id
        ):
            raise ValueError("sink must expose a non-empty string sink_id")
        self.note_id = note_id
        self.sink = sink
        self.cursors = cursors
        self.fetch_window = fetch_window
        self.cursor_id = cursor_id(link_token)
        self.cursor_key = f"{self.cursor_id}:{self.identity.ref}"
        self._committed_offset = 0

    def _cursor_record(self, committed_offset: int) -> dict:
        return {
            "v": 1,
            "cursor_id": self.cursor_id,
            "note_id": self.note_id,
            "ref": self.identity.ref,
            "raw_sha256": self.identity.raw_sha256,
            "total_size": self.identity.total_size,
            "committed_offset": committed_offset,
            "sink_id": self.sink.sink_id,
        }

    def _cursor_matches(self, record: object) -> bool:
        if not isinstance(record, dict) or set(record) != _CURSOR_FIELDS:
            return False
        committed = record.get("committed_offset")
        return (
            type(record.get("v")) is int
            and record["v"] == 1
            and record.get("cursor_id") == self.cursor_id
            and record.get("note_id") == self.note_id
            and record.get("ref") == self.identity.ref
            and record.get("raw_sha256") == self.identity.raw_sha256
            and type(record.get("total_size")) is int
            and record["total_size"] == self.identity.total_size
            and type(committed) is int
            and 0 <= committed <= self.identity.total_size
            and committed % CHUNK == 0
            and record.get("sink_id") == self.sink.sink_id
        )

    async def _prepare(self) -> int:
        record = self.cursors.get(self.cursor_key)
        if not self._cursor_matches(record):
            committed = 0
            await self.sink.truncate(0)
            self.cursors.put(self.cursor_key, self._cursor_record(0))
            self._committed_offset = 0
            return committed
        committed = record["committed_offset"]
        sink_size = getattr(self.sink, "size", None)
        if type(sink_size) is not int or sink_size < committed:
            # A cursor without all of its staged prefix cannot be resumed:
            # padding the missing bytes would silently splice zeros.
            committed = 0
            await self.sink.truncate(0)
            self.cursors.put(self.cursor_key, self._cursor_record(0))
            self._committed_offset = 0
            return committed
        await self.sink.truncate(committed)
        self._committed_offset = committed
        return committed

    async def _commit(self, offset: int) -> None:
        await self.sink.flush()
        self.cursors.put(self.cursor_key, self._cursor_record(offset))
        self._committed_offset = offset

    async def _response_stream(self, request: dict):
        response = self.fetch_window(request)
        if inspect.isawaitable(response):
            response = await response
        if response is None or not hasattr(response, "__aiter__"):
            raise AttachmentDisconnected("fetch produced no response stream")
        return response

    @staticmethod
    def _parse_error(message: bytes, ref: str) -> str | None:
        if not message.startswith(b"{"):
            return None
        try:
            value = json.loads(message)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AttachmentAssemblyError("malformed error message") from exc
        try:
            if canonical_json(value) != message:
                raise AttachmentAssemblyError(
                    "error message is not canonical JSON"
                )
        except (TypeError, ValueError) as exc:
            raise AttachmentAssemblyError("invalid error message") from exc
        allowed = {"v", "op", "ref", "code", "detail"}
        if (
            not isinstance(value, dict)
            or set(value) not in (allowed - {"detail"}, allowed)
            or type(value.get("v")) is not int
            or value.get("v") != 1
            or value.get("op") != "error"
            or value.get("ref") != ref
            or value.get("code") not in _ERROR_CODES
            or (
                "detail" in value
                and not isinstance(value.get("detail"), str)
            )
        ):
            raise AttachmentAssemblyError("invalid error message")
        return value["code"]

    async def _consume_window(self, start: int) -> tuple[int, bool]:
        total = self.identity.total_size
        limit = min(start + WINDOW, total)
        request = {
            "v": 1,
            "op": "attachment.fetch",
            "ref": self.identity.ref,
            "offset": start,
            "length": WINDOW,
        }
        response = await self._response_stream(request)
        expected = start
        saw_last = False
        saw_message = False
        close = getattr(response, "aclose", None)
        try:
            async for raw in response:
                saw_message = True
                if not isinstance(raw, (bytes, bytearray)):
                    raise AttachmentAssemblyError("channel message is not bytes")
                message = bytes(raw)
                if saw_last:
                    raise AttachmentAssemblyError(
                        "message followed LAST_IN_WINDOW"
                    )
                error = self._parse_error(message, self.identity.ref)
                if error is not None:
                    raise AttachmentRefused(error)
                if len(message) < 9:
                    raise AttachmentAssemblyError("body frame is too short")
                offset = int.from_bytes(message[:8], "big")
                flags = message[8]
                chunk = message[9:]
                if flags & ~_KNOWN_BODY_FLAGS:
                    raise AttachmentAssemblyError("unknown body flags")
                if offset != expected:
                    raise AttachmentAssemblyError(
                        "body frame is non-contiguous"
                    )
                if len(chunk) > CHUNK:
                    raise AttachmentAssemblyError("body chunk exceeds CHUNK")
                if total and not chunk:
                    raise AttachmentAssemblyError(
                        "empty body chunk before EOF"
                    )
                end = offset + len(chunk)
                if end > limit:
                    raise AttachmentAssemblyError(
                        "body frame exceeds requested window"
                    )
                last = bool(flags & LAST_IN_WINDOW)
                eof = bool(flags & EOF)
                if last != (end == limit):
                    raise AttachmentAssemblyError(
                        "LAST_IN_WINDOW does not match window end"
                    )
                if eof != (end == total):
                    raise AttachmentAssemblyError(
                        "EOF does not match total_size"
                    )
                if eof and not last:
                    raise AttachmentAssemblyError(
                        "EOF without LAST_IN_WINDOW"
                    )

                await self.sink.write_at(offset, chunk)
                expected = end
                saw_last = last
                # The final EOF message is committed by successful completion;
                # keeping it out of the cursor prevents an invalid extra
                # message from turning into a false completed resume.
                if expected < total and expected % CHUNK == 0:
                    await self._commit(expected)
        finally:
            if close is not None:
                try:
                    await close()
                except Exception:
                    # The exchange outcome is determined by the messages above.
                    # Closing a broken transport must not hide a typed refusal or
                    # assembly error, and no later window can be requested.
                    pass

        if not saw_message:
            raise AttachmentDisconnected("fetch response ended empty")
        if not saw_last:
            raise AttachmentAssemblyError("window ended without LAST_IN_WINDOW")
        return expected, expected == total

    async def run(self, *, max_windows: int | None = None) -> DownloadResult:
        """Resume and run until complete, refused, disconnected, or paused."""
        if max_windows is not None and (
            type(max_windows) is not int or max_windows <= 0
        ):
            raise ValueError("max_windows must be a positive integer")
        if self.identity.oversize:
            raise AttachmentRefused("oversize")
        if not self.cursors.acquire(self.cursor_key):
            raise AttachmentAlreadyActive(self.cursor_key)

        committed = 0
        windows = 0
        try:
            committed = await self._prepare()
            if self.identity.total_size > 0 and committed == self.identity.total_size:
                self.cursors.delete(self.cursor_key)
                return DownloadResult("complete", committed, windows)
            while True:
                try:
                    end, complete = await self._consume_window(committed)
                except AttachmentDownloadError:
                    await self.sink.truncate(self._committed_offset)
                    raise
                except Exception as exc:
                    await self.sink.truncate(self._committed_offset)
                    raise AttachmentDisconnected(str(exc)) from exc
                except BaseException:
                    # Cancellation/GeneratorExit must not leave staged bytes
                    # beyond the durable cursor. A later run would also
                    # truncate them in _prepare, but do it immediately.
                    await self.sink.truncate(self._committed_offset)
                    raise

                windows += 1
                if complete:
                    await self.sink.flush()
                    self.cursors.delete(self.cursor_key)
                    return DownloadResult("complete", end, windows)
                if end % CHUNK != 0:
                    await self.sink.truncate(self._committed_offset)
                    raise AttachmentAssemblyError(
                        "window ended off a commit boundary"
                    )
                committed = end
                # _consume_window committed every complete CHUNK before return.
                if max_windows is not None and windows >= max_windows:
                    return DownloadResult("paused", committed, windows)
        finally:
            self.cursors.release(self.cursor_key)

    async def cancel(self) -> None:
        """Discard staged bytes and cursor when no run is active."""
        if not self.cursors.acquire(self.cursor_key):
            raise AttachmentAlreadyActive(self.cursor_key)
        try:
            await self.sink.truncate(0)
            self.cursors.delete(self.cursor_key)
        finally:
            self.cursors.release(self.cursor_key)
