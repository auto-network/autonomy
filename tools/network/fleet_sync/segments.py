"""Immutable, independently decodable codec objects for RaptorQ input."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Iterable

from .codec import MAGIC, Mutation, _frame, _sort_key, encode_stream


@dataclass(frozen=True)
class Segment:
    ordinal: int
    payload: bytes
    digest: str
    mutation_count: int
    first_position: tuple[int, bytes]
    last_position: tuple[int, bytes]


def encode_segments(
    mutations: Iterable[Mutation],
    *,
    target_bytes: int = 4 * 1024 * 1024,
) -> list[Segment]:
    """Partition canonical frames without ever cutting a mutation frame.

    A single oversize mutation is emitted as one oversize segment and remains
    subject to the codec's independent 64 MiB frame ceiling.
    """

    if target_bytes <= len(MAGIC) + 8:
        raise ValueError("segment target is too small for codec framing")
    unique = {_frame(mutation): mutation for mutation in mutations}
    ordered = sorted(unique.values(), key=_sort_key)
    groups: list[list[Mutation]] = []
    current: list[Mutation] = []
    current_bytes = len(MAGIC) + 4
    for mutation in ordered:
        frame_bytes = 4 + len(_frame(mutation))
        if current and current_bytes + frame_bytes > target_bytes:
            groups.append(current)
            current = []
            current_bytes = len(MAGIC) + 4
        current.append(mutation)
        current_bytes += frame_bytes
    if current:
        groups.append(current)

    segments: list[Segment] = []
    for ordinal, group in enumerate(groups):
        payload = encode_stream(group)
        segments.append(Segment(
            ordinal=ordinal,
            payload=payload,
            digest=hashlib.sha256(payload).hexdigest(),
            mutation_count=len(group),
            first_position=group[0].position,
            last_position=group[-1].position,
        ))
    return segments
