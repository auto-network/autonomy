"""Order-independent mutation inbox used by stream and checkpoint paths."""

from __future__ import annotations

import hashlib
from typing import Iterable

from .codec import Mutation, encode_stream, encode_value


def _address_key(mutation: Mutation) -> bytes:
    return encode_value([mutation.table, list(mutation.address)])


class MutationInbox:
    """Merge mutations without touching foreign-key-constrained graph tables.

    Every streamed record and every decoded checkpoint segment enters this
    inbox first.  The winner for one logical address is the maximum
    ``(timestamp, candidate_hash)``; this makes ingestion associative,
    commutative, and idempotent, including tombstones.
    """

    def __init__(self) -> None:
        self._winners: dict[bytes, Mutation] = {}

    def ingest(self, mutations: Iterable[Mutation]) -> None:
        for candidate in mutations:
            key = _address_key(candidate)
            current = self._winners.get(key)
            if current is None or candidate.position > current.position:
                self._winners[key] = candidate

    def winners(self, *, include_tombstones: bool = True) -> list[Mutation]:
        mutations = list(self._winners.values())
        if not include_tombstones:
            mutations = [mutation for mutation in mutations if not mutation.tombstone]
        # Reuse the codec's canonical stream order rather than duplicate it.
        from .codec import decode_stream

        return decode_stream(encode_stream(mutations))

    def canonical_bytes(self, *, include_tombstones: bool = True) -> bytes:
        return encode_stream(self.winners(include_tombstones=include_tombstones))

    def digest(self, *, include_tombstones: bool = True) -> str:
        return hashlib.sha256(self.canonical_bytes(
            include_tombstones=include_tombstones
        )).hexdigest()

    def __len__(self) -> int:
        return len(self._winners)
