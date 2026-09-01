"""tls-stream/1 wire layer (auto-9z1xh) — the pure pieces shared by the
relay ingress and the connector adapter.

Wire contract: the r4 adapter seam frozen with the dashboard lane and
design of record graph://c880c5e6-8bd@4. Raw stream payloads ride ordinary
DATA frames (≤64 KiB) and bypass the RelayKit X25519 record layer — the
browser↔local-Caddy TLS *is* the content encryption; this layer supplies
framing, explicit credit, half-close/reset semantics, and the bounded
ClientHello/SNI peek the ingress routes on.

Nothing here logs, retains, or interprets payload bytes.
"""

from __future__ import annotations

import json
import struct

CAP_TLS_STREAM = "tls-stream/1"
STREAM_OPEN_VERSION = 1

STREAM_INITIAL_CREDIT = 256 * 1024
STREAM_MAX_BUFFER = 512 * 1024
STREAM_MAX_DATA = 64 * 1024
STREAM_MAX_PER_TUNNEL = 128
#: Bounded ClientHello peek: bytes and seconds.
SNI_PEEK_MAX_BYTES = 16 * 1024
STREAM_HANDSHAKE_TIMEOUT = 10.0
STREAM_IDLE_TIMEOUT = 600.0
#: Receivers batch credit replenishment at one max-size frame.
CREDIT_REPLENISH_BATCH = 64 * 1024

#: Reset / close codes (seam §4.3).
RESET_ORDERLY = 1
RESET_TIMEOUT = 2
RESET_OVERFLOW = 3
RESET_BYTE_BUDGET = 4
RESET_ROUTE_RELEASED = 5
RESET_TUNNEL_LOSS = 6
RESET_PROTOCOL = 7
_RESET_CODES = frozenset(range(1, 8))


class StreamProtocolError(Exception):
    """Bytes that violate the tls-stream/1 contract."""


class NeedMoreData(Exception):
    """A ClientHello peek that is so far incomplete, not invalid."""


# -- OPEN payload -----------------------------------------------------------


def build_stream_open(*, host: str, reservation: str, credit: int) -> bytes:
    """Relay → connector OPEN payload. Carries only routing context — no
    target, no grant (seam §4.1)."""
    return json.dumps({
        "kind": "tls-stream",
        "v": STREAM_OPEN_VERSION,
        "host": host,
        "reservation": reservation,
        "credit": credit,
    }).encode("utf-8")


def parse_stream_open(meta: dict) -> tuple[str, str, int]:
    """Validate an already-JSON-decoded OPEN payload of kind tls-stream.
    Returns (host, reservation, credit)."""
    if (
        not isinstance(meta, dict)
        or set(meta) != {"kind", "v", "host", "reservation", "credit"}
        or meta.get("kind") != "tls-stream"
        or meta.get("v") != STREAM_OPEN_VERSION
        or not isinstance(meta.get("host"), str)
        or not isinstance(meta.get("reservation"), str)
        or type(meta.get("credit")) is not int
        or not 0 < meta["credit"] <= STREAM_MAX_BUFFER
    ):
        raise StreamProtocolError("malformed tls-stream OPEN payload")
    return meta["host"], meta["reservation"], meta["credit"]


# -- STREAM_CTRL payloads ---------------------------------------------------


def build_ctrl_open_ok(*, credit: int) -> bytes:
    return json.dumps(
        {"op": "open-ok", "v": STREAM_OPEN_VERSION, "credit": credit}
    ).encode("utf-8")


def build_ctrl_credit(add: int) -> bytes:
    return json.dumps({"op": "credit", "add": add}).encode("utf-8")


def build_ctrl_eof() -> bytes:
    return json.dumps({"op": "eof"}).encode("utf-8")


def build_ctrl_reset(code: int) -> bytes:
    if code not in _RESET_CODES:
        raise StreamProtocolError(f"unknown reset code: {code!r}")
    return json.dumps({"op": "reset", "code": code}).encode("utf-8")


_CTRL_FIELDS = {
    "open-ok": {"op", "v", "credit"},
    "credit": {"op", "add"},
    "eof": {"op"},
    "reset": {"op", "code"},
}


def parse_ctrl(raw) -> dict:
    """Strictly parse one STREAM_CTRL payload."""
    try:
        data = json.loads(
            raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise StreamProtocolError("ctrl payload is not JSON") from exc
    if not isinstance(data, dict):
        raise StreamProtocolError("ctrl payload must be a JSON object")
    op = data.get("op")
    fields = _CTRL_FIELDS.get(op)
    if fields is None or set(data) != fields:
        raise StreamProtocolError(f"malformed ctrl op: {op!r}")
    if op == "open-ok":
        if data["v"] != STREAM_OPEN_VERSION or type(data["credit"]) is not int \
                or not 0 < data["credit"] <= STREAM_MAX_BUFFER:
            raise StreamProtocolError("malformed open-ok")
    elif op == "credit":
        if type(data["add"]) is not int or not 0 < data["add"] <= STREAM_MAX_BUFFER:
            raise StreamProtocolError("malformed credit grant")
    elif op == "reset":
        if data["code"] not in _RESET_CODES:
            raise StreamProtocolError("unknown reset code")
    return data


# -- bounded ClientHello / SNI peek ----------------------------------------

_TLS_HANDSHAKE = 0x16
_TLS_CLIENT_HELLO = 0x01
_EXT_SERVER_NAME = 0x0000


def extract_sni(buf: bytes) -> str | None:
    """Extract server_name from the buffered start of a TLS connection.

    Raises :class:`NeedMoreData` while the ClientHello is incomplete and
    :class:`StreamProtocolError` for bytes that are not a ClientHello.
    Returns ``None`` for a complete ClientHello without SNI. Handshake
    fragments spanning multiple records are reassembled; the caller bounds
    total buffering at :data:`SNI_PEEK_MAX_BYTES`.
    """
    if len(buf) < 5:
        raise NeedMoreData("no complete TLS record header yet")
    if buf[0] != _TLS_HANDSHAKE:
        raise StreamProtocolError("not a TLS handshake record")
    # Reassemble contiguous handshake-record payloads — but stop the
    # moment the ClientHello is complete: bytes after it (a coalesced
    # client flight) are opaque payload, never parsed here.
    handshake = b""
    offset = 0

    def _complete() -> bool:
        return (
            len(handshake) >= 4
            and len(handshake) >= 4 + int.from_bytes(handshake[1:4], "big")
        )

    while not _complete():
        if len(buf) - offset < 5:
            raise NeedMoreData("next TLS record header incomplete")
        rec_type = buf[offset]
        rec_len = struct.unpack(">H", buf[offset + 3:offset + 5])[0]
        if rec_type != _TLS_HANDSHAKE:
            raise StreamProtocolError(
                "non-handshake record before ClientHello end"
            )
        end = offset + 5 + rec_len
        handshake += buf[offset + 5:min(end, len(buf))]
        offset = end
        if len(handshake) >= 1 and handshake[0] != _TLS_CLIENT_HELLO:
            raise StreamProtocolError("handshake is not a ClientHello")
        if offset > len(buf) and not _complete():
            raise NeedMoreData("ClientHello body incomplete")
    body_len = int.from_bytes(handshake[1:4], "big")
    body = handshake[4:4 + body_len]

    def _take(n: int, at: int) -> tuple[bytes, int]:
        if at + n > len(body):
            raise StreamProtocolError("truncated ClientHello structure")
        return body[at:at + n], at + n

    pos = 34  # client_version(2) + random(32)
    if pos > len(body):
        raise StreamProtocolError("truncated ClientHello structure")
    sid_len, pos = body[pos:pos + 1], pos + 1
    if not sid_len:
        raise StreamProtocolError("truncated ClientHello structure")
    _, pos = _take(sid_len[0], pos)
    cs_raw, pos = _take(2, pos)
    _, pos = _take(struct.unpack(">H", cs_raw)[0], pos)
    comp_raw, pos = _take(1, pos)
    _, pos = _take(comp_raw[0], pos)
    if pos == len(body):
        return None  # legal: no extensions at all
    ext_raw, pos = _take(2, pos)
    ext_end = pos + struct.unpack(">H", ext_raw)[0]
    if ext_end > len(body):
        raise StreamProtocolError("truncated extensions block")
    while pos + 4 <= ext_end:
        ext_type = struct.unpack(">H", body[pos:pos + 2])[0]
        ext_len = struct.unpack(">H", body[pos + 2:pos + 4])[0]
        data_start = pos + 4
        if data_start + ext_len > ext_end:
            raise StreamProtocolError("truncated extension")
        if ext_type == _EXT_SERVER_NAME:
            data = body[data_start:data_start + ext_len]
            if len(data) < 5 or data[2] != 0x00:
                raise StreamProtocolError("malformed server_name extension")
            name_len = struct.unpack(">H", data[3:5])[0]
            if 5 + name_len > len(data):
                raise StreamProtocolError("malformed server_name extension")
            try:
                return data[5:5 + name_len].decode("ascii")
            except UnicodeDecodeError as exc:
                raise StreamProtocolError("non-ASCII server_name") from exc
        pos = data_start + ext_len
    return None


# -- credit accounting ------------------------------------------------------


class CreditWindow:
    """The sender's view of its grant: it may never have more consumed
    (sent) bytes outstanding than the peer granted."""

    def __init__(self, initial: int):
        self._sendable = int(initial)

    @property
    def sendable(self) -> int:
        return self._sendable

    def consume(self, n: int) -> None:
        if n > self._sendable:
            raise StreamProtocolError("credit violated: send exceeds grant")
        self._sendable -= n

    def grant(self, n: int) -> None:
        self._sendable += n


class ReplenishTracker:
    """The receiver's batching of credit grants: accumulate consumed
    bytes and emit one grant once a batch is reached."""

    def __init__(self, batch: int = CREDIT_REPLENISH_BATCH):
        self._batch = int(batch)
        self._pending = 0

    def consumed(self, n: int) -> int | None:
        self._pending += int(n)
        if self._pending >= self._batch:
            grant, self._pending = self._pending, 0
            return grant
        return None
