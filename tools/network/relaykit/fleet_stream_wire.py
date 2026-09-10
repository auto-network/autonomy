"""fleet-directed-stream/1 wire layer (auto-fh2nv) — the pure pieces shared
by the relay's pair broker and the connector's endpoint.

A directed stream joins two AUTHENTICATED outbound tunnels of one
organization at the relay: the source names an exact destination slot
``(persona_pub, machine)``, the relay resolves it through
``directed_pair.resolve_directed_pair`` and forwards frames between the two
legs. It extends ``tls-stream/1`` rather than replacing it: the same
FRAME_OPEN / FRAME_DATA / FRAME_STREAM_CTRL / FRAME_CLOSE frames, the same
``eof`` and ``reset`` controls, the same 64 KiB DATA cap. What is new is
exactly what a two-leg, peer-to-peer carrier needs and a public TCP bridge
does not:

* ``fleet-open-ok`` carries a byte window AND a slot window. Byte credit
  alone lets a peer retain 262,144 one-byte descriptors inside a 256 KiB
  grant; the slot window bounds descriptors on the wire, so the relay never
  has to reject a compliant small write to stay bounded.
* ``fleet-ready`` is sent by the relay to BOTH legs after both accepted and
  the pair re-validated. A leg may send only after its own READY; it may
  receive from its own open-ok onward (the peer told first will send while
  this leg's READY is still in flight).
* ``fleet-credit`` returns bytes AND slots, and must name an exact FIFO
  prefix of the frames outstanding on that leg: the relay matches receipts
  one-to-one and forwards the identical grant to the sender, so custody is
  conserved per direction and a mismatch is a protocol reset, never a
  double refund.

DATA is opaque to the relay: it forwards each source frame as one
destination frame, never splitting or coalescing, so slot accounting is
exact by construction. The ENDPOINT owns message boundaries: the fleet
record layer above needs whole messages (a 128 KiB ChannelCrypto record
does not fit one DATA frame), so the endpoint prefixes each message with its
length and reassembles across frames, bounded by FLEET_STREAM_MAX_MESSAGE.

Every numeric value here is a provisional engineering default, env-
overridable like the tls-stream/1 knobs, and NOT an approved policy: the
contract freezes them only after two-leg measurement.
"""

from __future__ import annotations

import json
import re
import struct

from .stream_wire import (
    STREAM_MAX_DATA,
    StreamProtocolError,
    _RESET_CODES,
    _env_int,
)

CAP_FLEET_DIRECTED_STREAM = "fleet-directed-stream/1"
FLEET_STREAM_VERSION = 1
FLEET_STREAM_KIND = "fleet-stream"

#: The receive window an endpoint offers by default, per direction.
#: Env: AUTONOMY_FLEET_STREAM_WINDOW_BYTES / _SLOTS.
FLEET_STREAM_WINDOW_BYTES = _env_int("AUTONOMY_FLEET_STREAM_WINDOW_BYTES", 256 * 1024)
FLEET_STREAM_WINDOW_SLOTS = _env_int("AUTONOMY_FLEET_STREAM_WINDOW_SLOTS", 32)
#: The most a leg may offer; the relay refuses larger open-oks because the
#: offer is what bounds relay custody for that direction.
FLEET_STREAM_MAX_WINDOW_BYTES = _env_int("AUTONOMY_FLEET_STREAM_MAX_WINDOW_BYTES", 1024 * 1024)
FLEET_STREAM_MAX_WINDOW_SLOTS = _env_int("AUTONOMY_FLEET_STREAM_MAX_WINDOW_SLOTS", 256)
#: Largest reassembled endpoint message. The record layer emits 128 KiB
#: records; this leaves room for framing and the hellos, and bounds what a
#: peer can make an endpoint retain before a message completes.
FLEET_STREAM_MAX_MESSAGE = _env_int("AUTONOMY_FLEET_STREAM_MAX_MESSAGE", 256 * 1024)
#: Admission caps, counted BEFORE any pair state is allocated.
FLEET_PAIRS_PER_TUNNEL = _env_int("AUTONOMY_FLEET_PAIRS_PER_TUNNEL", 64)
FLEET_PAIRS_PER_PROCESS = _env_int("AUTONOMY_FLEET_PAIRS_PER_PROCESS", 256)
#: Seconds the relay waits for both open-oks before resetting the offer.
FLEET_OPEN_DEADLINE_S = float(_env_int("AUTONOMY_FLEET_OPEN_DEADLINE_S", 10))

_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"^[0-9a-f]{32}\Z")

ROLE_SOURCE = "source"
ROLE_DESTINATION = "destination"


def _hex(value: object, pattern: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or pattern.match(value) is None:
        raise StreamProtocolError(f"malformed {what}")
    return value


def _window(value: object, what: str) -> tuple[int, int]:
    if (
        not isinstance(value, dict)
        or set(value) != {"bytes", "slots"}
        or type(value["bytes"]) is not int
        or type(value["slots"]) is not int
        or not 0 < value["bytes"] <= FLEET_STREAM_MAX_WINDOW_BYTES
        or not 0 < value["slots"] <= FLEET_STREAM_MAX_WINDOW_SLOTS
    ):
        raise StreamProtocolError(f"malformed {what} window")
    return value["bytes"], value["slots"]


# -- the source's request: a control op, not a frame ------------------------


def parse_fleet_open_args(args: object) -> dict:
    """Validate the ``fleet-open`` control op arguments the source sends.

    ``claimed_machine_pub`` is the source's DURABLE fleet key as an
    UNVERIFIED hint for the destination's expected-peer check; the relay
    forwards it verbatim and asserts nothing about it. A liar only makes
    its own fleet handshake fail.
    """
    if not isinstance(args, dict):
        raise StreamProtocolError("fleet-open args must be an object")
    allowed = {"dst_persona_pub", "dst_machine", "operation_id", "claimed_machine_pub"}
    required = {"dst_persona_pub", "dst_machine", "operation_id"}
    if not required <= set(args) <= allowed:
        raise StreamProtocolError("fleet-open args have the wrong fields")
    out = {
        "dst_persona_pub": _hex(args["dst_persona_pub"], _HEX64, "dst_persona_pub"),
        "dst_machine": _hex(args["dst_machine"], _HEX64, "dst_machine"),
        "operation_id": _hex(args["operation_id"], _HEX32, "operation_id"),
        "claimed_machine_pub": None,
    }
    if "claimed_machine_pub" in args:
        out["claimed_machine_pub"] = _hex(
            args["claimed_machine_pub"], _HEX64, "claimed_machine_pub"
        )
    return out


# -- OPEN (relay → each leg) -----------------------------------------------


def build_fleet_open(
    *, pair_id: str, leg_nonce: str, role: str, operation_id: str,
    peer_persona_pub: str, peer_machine: str,
    claimed_machine_pub: str | None,
) -> bytes:
    """FRAME_OPEN payload toward one leg. Carries routing facts only: no
    credit (that arrives in READY, once the peer has offered its window)
    and no grant of any authority."""
    if role not in (ROLE_SOURCE, ROLE_DESTINATION):
        raise StreamProtocolError("unknown leg role")
    return json.dumps({
        "kind": FLEET_STREAM_KIND,
        "v": FLEET_STREAM_VERSION,
        "pair_id": pair_id,
        "leg_nonce": leg_nonce,
        "role": role,
        "operation_id": operation_id,
        "peer": {"persona_pub": peer_persona_pub, "machine": peer_machine},
        "claimed_machine_pub": claimed_machine_pub,
    }).encode("utf-8")


def parse_fleet_open(meta: object) -> dict:
    """Validate an already-decoded FRAME_OPEN payload of kind fleet-stream."""
    if (
        not isinstance(meta, dict)
        or set(meta) != {
            "kind", "v", "pair_id", "leg_nonce", "role", "operation_id",
            "peer", "claimed_machine_pub",
        }
        or meta["kind"] != FLEET_STREAM_KIND
        or meta["v"] != FLEET_STREAM_VERSION
        or meta["role"] not in (ROLE_SOURCE, ROLE_DESTINATION)
        or not isinstance(meta["peer"], dict)
        or set(meta["peer"]) != {"persona_pub", "machine"}
    ):
        raise StreamProtocolError("malformed fleet-stream OPEN payload")
    claimed = meta["claimed_machine_pub"]
    return {
        "pair_id": _hex(meta["pair_id"], _HEX32, "pair_id"),
        "leg_nonce": _hex(meta["leg_nonce"], _HEX32, "leg_nonce"),
        "role": meta["role"],
        "operation_id": _hex(meta["operation_id"], _HEX32, "operation_id"),
        "peer_persona_pub": _hex(meta["peer"]["persona_pub"], _HEX64, "peer persona"),
        "peer_machine": _hex(meta["peer"]["machine"], _HEX64, "peer machine"),
        "claimed_machine_pub": (
            None if claimed is None
            else _hex(claimed, _HEX64, "claimed_machine_pub")
        ),
    }


# -- STREAM_CTRL payloads ---------------------------------------------------


def build_fleet_open_ok(*, nonce: str, bytes_: int, slots: int) -> bytes:
    """Leg → relay: this leg installed a dormant endpoint and offers a
    receive window. Echoes its own leg nonce so a stale open-ok for a
    superseded offer is distinguishable."""
    return json.dumps({
        "op": "fleet-open-ok", "v": FLEET_STREAM_VERSION, "nonce": nonce,
        "window": {"bytes": int(bytes_), "slots": int(slots)},
    }).encode("utf-8")


def build_fleet_ready(
    *, pair_id: str, source_nonce: str, destination_nonce: str,
    bytes_: int, slots: int,
) -> bytes:
    """Relay → leg: both legs accepted and the pair re-validated. The window
    is the PEER's offer, i.e. this leg's send window. Both nonces are
    carried so both endpoints can bind the same session string."""
    return json.dumps({
        "op": "fleet-ready", "pair_id": pair_id,
        "source_nonce": source_nonce, "destination_nonce": destination_nonce,
        "window": {"bytes": int(bytes_), "slots": int(slots)},
    }).encode("utf-8")


def build_fleet_credit(*, bytes_: int, slots: int) -> bytes:
    """Receiver → relay → sender: exactly *slots* whole frames totalling
    *bytes_* were consumed, in FIFO order."""
    return json.dumps({
        "op": "fleet-credit", "bytes": int(bytes_), "slots": int(slots),
    }).encode("utf-8")


_FLEET_CTRL_FIELDS = {
    "fleet-open-ok": {"op", "v", "nonce", "window"},
    "fleet-ready": {"op", "pair_id", "source_nonce", "destination_nonce", "window"},
    "fleet-credit": {"op", "bytes", "slots"},
    "eof": {"op"},
    "reset": {"op", "code"},
}


def parse_fleet_ctrl(raw) -> dict:
    """Strictly parse one STREAM_CTRL payload on a fleet stream.

    Deliberately separate from ``stream_wire.parse_ctrl``: the public
    tls-stream/1 parser and its vectors stay byte-for-byte unchanged, and a
    fleet op arriving on a public stream is still a protocol error there.
    """
    try:
        data = json.loads(
            raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise StreamProtocolError("ctrl payload is not JSON") from exc
    if not isinstance(data, dict):
        raise StreamProtocolError("ctrl payload must be a JSON object")
    op = data.get("op")
    fields = _FLEET_CTRL_FIELDS.get(op)
    if fields is None or set(data) != fields:
        raise StreamProtocolError(f"malformed fleet ctrl op: {op!r}")
    if op == "fleet-open-ok":
        if data["v"] != FLEET_STREAM_VERSION:
            raise StreamProtocolError("malformed fleet-open-ok")
        _hex(data["nonce"], _HEX32, "open-ok nonce")
        data["window"] = dict(zip(("bytes", "slots"), _window(data["window"], "open-ok")))
    elif op == "fleet-ready":
        _hex(data["pair_id"], _HEX32, "ready pair_id")
        _hex(data["source_nonce"], _HEX32, "ready source_nonce")
        _hex(data["destination_nonce"], _HEX32, "ready destination_nonce")
        data["window"] = dict(zip(("bytes", "slots"), _window(data["window"], "ready")))
    elif op == "fleet-credit":
        if (
            type(data["bytes"]) is not int or type(data["slots"]) is not int
            or data["bytes"] <= 0 or data["slots"] <= 0
            or data["bytes"] > FLEET_STREAM_MAX_WINDOW_BYTES
            or data["slots"] > FLEET_STREAM_MAX_WINDOW_SLOTS
            or data["bytes"] < data["slots"]  # every frame is at least one byte
        ):
            raise StreamProtocolError("malformed fleet-credit")
    elif op == "reset":
        if data["code"] not in _RESET_CODES:
            raise StreamProtocolError("unknown reset code")
    return data


# -- credit accounting ------------------------------------------------------


class FleetWindow:
    """The sender's view of a byte+slot grant: it may never have more sent
    bytes or frames outstanding than the peer granted."""

    def __init__(self, bytes_: int = 0, slots: int = 0):
        self.bytes = int(bytes_)
        self.slots = int(slots)

    def can_send(self, n: int) -> bool:
        return 0 < n <= STREAM_MAX_DATA and n <= self.bytes and self.slots >= 1

    def consume(self, n: int) -> None:
        if not self.can_send(n):
            raise StreamProtocolError("credit violated: send exceeds grant")
        self.bytes -= n
        self.slots -= 1

    def grant(self, bytes_: int, slots: int) -> None:
        self.bytes += int(bytes_)
        self.slots += int(slots)


class ReceiptLedger:
    """The relay's FIFO of frames forwarded to a receiver and not yet
    credited back. A credit must name an exact prefix: exactly *slots*
    receipts whose lengths sum to *bytes*."""

    def __init__(self):
        self._lengths: list[int] = []
        self.outstanding_bytes = 0

    def __len__(self) -> int:
        return len(self._lengths)

    def forwarded(self, n: int) -> None:
        self._lengths.append(int(n))
        self.outstanding_bytes += int(n)

    def credited(self, bytes_: int, slots: int) -> None:
        """Remove exactly *slots* receipts totalling *bytes_*, or raise —
        any overshoot, undershoot, wrong count, or over-release is a
        protocol violation that resets the pair."""
        if slots <= 0 or slots > len(self._lengths):
            raise StreamProtocolError("credit names more frames than outstanding")
        prefix = self._lengths[:slots]
        if sum(prefix) != bytes_:
            raise StreamProtocolError("credit bytes do not match the frame prefix")
        del self._lengths[:slots]
        self.outstanding_bytes -= bytes_


# -- endpoint message framing ----------------------------------------------

_LEN = struct.Struct(">I")


def encode_message(payload: bytes) -> bytes:
    if len(payload) > FLEET_STREAM_MAX_MESSAGE:
        raise StreamProtocolError("message exceeds the fleet stream maximum")
    return _LEN.pack(len(payload)) + payload


class MessageAssembler:
    """Reassemble length-prefixed messages from an ordered byte stream while
    tracking which DATA frames each completed message consumed, so the
    receiver can credit exactly those frames after delivery.

    ``feed`` records one frame; ``next_message`` returns the next complete
    message together with the count and byte total of frames that are now
    FULLY consumed (a frame straddling a message boundary is credited with
    the later message). Retained bytes never exceed one maximum message plus
    one maximum frame.
    """

    def __init__(self, max_message: int = FLEET_STREAM_MAX_MESSAGE):
        self._max = int(max_message)
        self._buf = bytearray()
        self._frames: list[int] = []   # lengths of frames fed, FIFO
        self._consumed = 0             # bytes of self._frames[0] already taken
        self.retained_bytes = 0

    def feed(self, frame: bytes) -> None:
        if not frame:
            return
        self._buf.extend(frame)
        self._frames.append(len(frame))
        self.retained_bytes += len(frame)

    def _take(self, n: int) -> tuple[int, int]:
        """Account *n* consumed bytes against the frame FIFO. Returns the
        (frames, bytes) that became fully consumed."""
        frames = 0
        bytes_ = 0
        remaining = n
        while remaining > 0:
            head = self._frames[0]
            available = head - self._consumed
            if remaining >= available:
                remaining -= available
                frames += 1
                bytes_ += head
                self._frames.pop(0)
                self._consumed = 0
            else:
                self._consumed += remaining
                remaining = 0
        return frames, bytes_

    def next_message(self) -> tuple[bytes, int, int] | None:
        if len(self._buf) < _LEN.size:
            return None
        (length,) = _LEN.unpack_from(self._buf, 0)
        if length > self._max:
            raise StreamProtocolError("peer message exceeds the fleet stream maximum")
        if len(self._buf) < _LEN.size + length:
            return None
        message = bytes(self._buf[_LEN.size:_LEN.size + length])
        del self._buf[:_LEN.size + length]
        frames, bytes_ = self._take(_LEN.size + length)
        self.retained_bytes -= _LEN.size + length
        return message, frames, bytes_
