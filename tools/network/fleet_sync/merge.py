"""Order-independent mutation inbox used by the stream and sweep paths."""

from __future__ import annotations

import hashlib
from typing import Iterable

from .codec import Mutation, encode_stream, encode_value
from .policies import PolicyKind, TABLE_POLICIES


class MutationConflictError(ValueError):
    """Two immutable records claim the same logical address."""


def mutation_wins(current: Mutation, candidate: Mutation) -> bool:
    """Return whether *candidate* replaces *current*, or fail on corruption."""

    if (current.table, current.address) != (candidate.table, candidate.address):
        raise ValueError("mutations do not share a logical address")
    policy = TABLE_POLICIES[current.table]
    if policy.kind not in {PolicyKind.IMMUTABLE, PolicyKind.IMMUTABLE_PRUNABLE}:
        return candidate.position > current.position
    if current.tombstone or candidate.tombstone:
        raise MutationConflictError("immutable record cannot be tombstoned")
    if current.values == candidate.values:
        return candidate.position > current.position
    if policy.kind is PolicyKind.IMMUTABLE_PRUNABLE:
        old = dict(current.values)
        new = dict(candidate.values)
        if set(old) == set(new) and all(
            old[column] == new[column] for column in old if column != "wire"
        ):
            old_wire = old.get("wire")
            new_wire = new.get("wire")
            if old_wire is None and new_wire is not None:
                return True
            if old_wire is not None and new_wire is None:
                return False
    raise MutationConflictError(
        f"immutable {current.table} records conflict at {current.address!r}"
    )


def _address_key(mutation: Mutation) -> bytes:
    return encode_value([mutation.table, list(mutation.address)])


class MutationInbox:
    """Merge mutations without touching foreign-key-constrained graph tables.

    Every streamed record and every swept page entry enters this
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
            if current is None or mutation_wins(current, candidate):
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
