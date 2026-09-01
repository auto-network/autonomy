"""Tunnel mux framing — spec open question Q1, decided here.

**Decision: raw WS binary frames with a fixed 17-byte header, no mux
library.** WebSocket is already message-oriented, so framing comes for
free; all the mux needs is a type tag and a channel id. A library
(yamux/mux variants) would add a dependency and stream semantics we
don't want — channels are independent WS-message sequences, not byte
streams.

Wire format of every binary message on the tunnel::

    [1B type][16B channel_id][payload...]

Types:

- ``FRAME_OPEN``  (0x01, relay → dashboard): a viewer arrived; payload is
  canonical JSON ``{"token": ...}``.
- ``FRAME_DATA``  (0x02, both directions): opaque channel bytes. The
  relay NEVER looks past the header (I5: ciphertext only).
- ``FRAME_CLOSE`` (0x03, both directions): channel ended; payload is an
  optional 2-byte big-endian code.
- ``FRAME_CTRL``  (0x04, both directions): an org-level control message
  riding the authenticated tunnel (register D19). ``channel_id`` is the
  reserved :data:`CTRL_CHANNEL_ID` — there is no viewer channel. Payload
  is canonical JSON: request ``{"id": <32-hex>, "op": ..., "args": {...}}``,
  reply ``{"id": <same>, "ok": true, ...}`` or
  ``{"id", "ok": false, "error": ...}``.

Channel ids are 16 CSPRNG bytes minted by the relay per viewer
connection (``new_channel_id`` collides with the reserved control id
only with negligible probability, and the relay never routes
``FRAME_CTRL`` to a viewer). Payload size is bounded by the channel
layer's chunking (``channel.CHUNK_SIZE``), so tunnel messages stay well
under WS ``max_size`` limits (Q3).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

FRAME_OPEN = 0x01
FRAME_DATA = 0x02
FRAME_CLOSE = 0x03
FRAME_CTRL = 0x04
#: Per-stream control for negotiated raw streams (tls-stream/1,
#: auto-9z1xh): open-ok / credit / eof / reset ride this type on the
#: stream's channel id. Capability-gated — never sent to a peer that did
#: not negotiate the cap, so pre-stream peers keep refusing it strictly.
FRAME_STREAM_CTRL = 0x05

_TYPES = frozenset(
    {FRAME_OPEN, FRAME_DATA, FRAME_CLOSE, FRAME_CTRL, FRAME_STREAM_CTRL}
)

CHANNEL_ID_LEN = 16
HEADER_LEN = 1 + CHANNEL_ID_LEN

#: The reserved channel id every FRAME_CTRL message carries — control
#: messages belong to the tunnel's org, not to any viewer channel.
CTRL_CHANNEL_ID = b"\x00" * CHANNEL_ID_LEN


class FrameError(ValueError):
    """A tunnel message that does not parse as a valid frame."""


@dataclass(frozen=True)
class Frame:
    type: int
    channel_id: bytes
    payload: bytes


def new_channel_id() -> bytes:
    return secrets.token_bytes(CHANNEL_ID_LEN)


def encode_frame(frame_type: int, channel_id: bytes, payload: bytes = b"") -> bytes:
    if frame_type not in _TYPES:
        raise FrameError(f"unknown frame type: {frame_type:#x}")
    if len(channel_id) != CHANNEL_ID_LEN:
        raise FrameError(f"channel_id must be {CHANNEL_ID_LEN} bytes")
    return bytes([frame_type]) + channel_id + payload


def decode_frame(raw: bytes) -> Frame:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < HEADER_LEN:
        raise FrameError("tunnel message shorter than frame header")
    frame_type = raw[0]
    if frame_type not in _TYPES:
        raise FrameError(f"unknown frame type: {frame_type:#x}")
    return Frame(
        type=frame_type,
        channel_id=bytes(raw[1:HEADER_LEN]),
        payload=bytes(raw[HEADER_LEN:]),
    )


# ── viewer-socket message kinds ─────────────────────────────────────────
#
# A DIFFERENT LAYER from the frame types above. Those mux viewer channels
# down an org's tunnel (connector <-> registry). These tag every message
# sent TO a viewer on the public socket, which carries two things decoded
# by two different keys:
#
#   RECORD  a pairwise channel record: [8-byte seq][ciphertext], opened
#           with this channel's key, strictly ordered.
#   FEED    a fan-out frame: [12-byte nonce][ciphertext], opened with the
#           link's shared stream key, unsolicited and unordered.
#
# Every message is tagged, including the handshake. The reader switches on
# the kind and never inspects the payload to guess. Deliberately NOT
# inferred from any property of the record format: tying the framing layer
# to the record layer's sequence ceiling would make a change to one
# silently misroute the other.
#
# Viewer -> registry is untagged: it carries records and nothing else.
VIEWER_KIND_RECORD = 0x00
VIEWER_KIND_FEED = 0x01
VIEWER_KIND_LEN = 1

_VIEWER_KINDS = frozenset({VIEWER_KIND_RECORD, VIEWER_KIND_FEED})


def tag_viewer_message(kind: int, payload: bytes) -> bytes:
    """Prefix *payload* with its one-byte viewer-socket kind."""
    if kind not in _VIEWER_KINDS:
        raise FrameError(f"unknown viewer message kind: {kind:#x}")
    return bytes([kind]) + payload


def split_viewer_message(raw: bytes) -> tuple[int, bytes]:
    """``(kind, payload)`` from a tagged message sent to a viewer."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < VIEWER_KIND_LEN:
        raise FrameError("viewer message is empty")
    kind = raw[0]
    if kind not in _VIEWER_KINDS:
        raise FrameError(f"unknown viewer message kind: {kind:#x}")
    return kind, bytes(raw[VIEWER_KIND_LEN:])
