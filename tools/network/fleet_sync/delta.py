"""Authored-mutation frame codec shared by the delta and sweep wire paths."""

from __future__ import annotations

import struct

from .codec import (
    CodecError,
    MAX_FRAME_BYTES,
    decode_mutation_frame,
    decode_value,
    encode_mutation_frame,
    encode_value,
)
from .compaction import AuthoredMutation


DELTA_FRAME_VERSION = 1
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
