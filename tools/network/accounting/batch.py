"""Immutable, organization-only official usage batch (v1).

One batch is the physical usage observed by one stable producer for one
organization in one epoch-aligned five-minute interval.  Its identity excludes
the counters and creation time on purpose: rebuilding the same logical batch
with different content produces the same ``batch_id`` and a different
``checksum``, which lets the sink fail closed on mutation instead of treating it
as more usage.

The producer is an Ed25519 public key.  The signature proves which producer
created the bytes; the future sink separately authorizes that key for the named
organization and resource families.  No member, session, link, token, address,
TURN allocation, or content identity has a field in this schema.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from tools.network.idkit import (
    KeyPair,
    canonical_json,
    load_public_key,
    verify_signature,
)
from tools.network.idkit.errors import IdkitError

USAGE_BATCH_VERSION = 1
BATCH_INTERVAL_SECONDS = 5 * 60
MAX_COUNTERS = 64
MAX_WIRE_BYTES = 16 * 1024
MAX_INT = 2**63 - 1

BATCH_ID_DOMAIN = b"autonomy.network.usage-batch-id.v1\n"
CHECKSUM_DOMAIN = b"autonomy.network.usage-batch-checksum.v1\n"
SIGNATURE_DOMAIN = b"autonomy.network.usage-batch-signature.v1\n"

_COUNTER_RE = re.compile(r"[a-z][a-z0-9]*(?:[._][a-z0-9]+)*")
_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_HEX_128_RE = re.compile(r"[0-9a-f]{128}")
_WIRE_FIELDS = frozenset(
    {
        "v",
        "batch_id",
        "producer",
        "organization_id",
        "sequence",
        "interval_start",
        "interval_end",
        "created_at",
        "counters",
        "checksum",
        "signature",
    }
)


class UsageBatchError(ValueError):
    """Base class for all usage-batch rejection paths."""


class UsageBatchMalformed(UsageBatchError):
    """The wire object is not the single valid v1 representation."""


class UsageBatchSignatureError(UsageBatchError):
    """The producer signature does not verify."""


def _strict_int(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageBatchMalformed(f"{name} must be an integer")
    if value < minimum or value > MAX_INT:
        raise UsageBatchMalformed(f"{name} must be between {minimum} and {MAX_INT}")
    return value


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise UsageBatchMalformed("organization_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UsageBatchMalformed("organization_id must be a canonical UUID") from exc
    if parsed.version is None or str(parsed) != value:
        raise UsageBatchMalformed("organization_id must be a canonical UUID")
    return value


def _producer_key(value: object) -> str:
    if not isinstance(value, str):
        raise UsageBatchMalformed("producer must be an Ed25519 public key")
    try:
        load_public_key(value)
    except IdkitError as exc:
        raise UsageBatchMalformed("producer must be an Ed25519 public key") from exc
    return value


def _counter_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise UsageBatchMalformed("counters must be an object")
    if not value or len(value) > MAX_COUNTERS:
        raise UsageBatchMalformed(f"counters must contain 1 to {MAX_COUNTERS} entries")
    counters: dict[str, int] = {}
    any_positive = False
    for name, raw_count in value.items():
        if (
            not isinstance(name, str)
            or len(name) > 96
            or _COUNTER_RE.fullmatch(name) is None
        ):
            raise UsageBatchMalformed(f"invalid physical counter name: {name!r}")
        count = _strict_int(raw_count, f"counter {name}")
        counters[name] = count
        any_positive = any_positive or count > 0
    if not any_positive:
        raise UsageBatchMalformed("a usage batch must contain positive physical usage")
    return dict(sorted(counters.items()))


def _identity_dict(
    *,
    producer: str,
    organization_id: str,
    sequence: int,
    interval_start: int,
    interval_end: int,
) -> dict:
    return {
        "v": USAGE_BATCH_VERSION,
        "producer": producer,
        "organization_id": organization_id,
        "sequence": sequence,
        "interval_start": interval_start,
        "interval_end": interval_end,
    }


def _batch_id(identity: dict) -> str:
    return hashlib.sha256(BATCH_ID_DOMAIN + canonical_json(identity)).hexdigest()


def _checksum(unsigned: dict) -> str:
    return hashlib.sha256(CHECKSUM_DOMAIN + canonical_json(unsigned)).hexdigest()


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise UsageBatchMalformed(f"duplicate JSON field: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class UsageBatch:
    """A validated and signature-verified immutable v1 usage batch."""

    producer: str
    organization_id: str
    sequence: int
    interval_start: int
    interval_end: int
    created_at: int
    counters: Mapping[str, int]
    batch_id: str
    checksum: str
    signature: str

    @classmethod
    def create(
        cls,
        *,
        signer: KeyPair,
        organization_id: str,
        sequence: int,
        interval_start: int,
        interval_end: int,
        created_at: int,
        counters: Mapping[str, int],
    ) -> "UsageBatch":
        """Create and sign one closed interval.

        Callers must durably store :meth:`to_json` once.  A retry sends those
        exact bytes; it must not call ``create`` again with a new timestamp.
        """
        values = cls._validated_values(
            producer=signer.public_hex,
            organization_id=organization_id,
            sequence=sequence,
            interval_start=interval_start,
            interval_end=interval_end,
            created_at=created_at,
            counters=dict(counters),
        )
        identity = _identity_dict(
            **{
                key: values[key]
                for key in (
                    "producer",
                    "organization_id",
                    "sequence",
                    "interval_start",
                    "interval_end",
                )
            }
        )
        batch_id = _batch_id(identity)
        unsigned = cls._unsigned_dict(batch_id=batch_id, **values)
        checksum = _checksum(unsigned)
        signed = {**unsigned, "checksum": checksum}
        signature = signer.sign_hex(SIGNATURE_DOMAIN + canonical_json(signed))
        return cls._construct(
            **values,
            batch_id=batch_id,
            checksum=checksum,
            signature=signature,
        )

    @classmethod
    def from_json(cls, wire: bytes | str) -> "UsageBatch":
        """Parse the one canonical wire form and verify identity, checksum, signature."""
        if isinstance(wire, str):
            try:
                encoded = wire.encode("ascii")
            except UnicodeEncodeError as exc:
                raise UsageBatchMalformed("usage batch wire must be ASCII") from exc
        elif isinstance(wire, bytes):
            encoded = wire
        else:
            raise UsageBatchMalformed("usage batch wire must be bytes or string")
        if not encoded or len(encoded) > MAX_WIRE_BYTES:
            raise UsageBatchMalformed(
                f"usage batch wire must contain 1 to {MAX_WIRE_BYTES} bytes"
            )
        try:
            raw = json.loads(
                encoded.decode("ascii"), object_pairs_hook=_pairs_without_duplicates
            )
        except UsageBatchMalformed:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageBatchMalformed("usage batch is not valid canonical JSON") from exc
        if not isinstance(raw, dict):
            raise UsageBatchMalformed("usage batch must be a JSON object")
        try:
            canonical_wire = canonical_json(raw)
        except IdkitError as exc:
            raise UsageBatchMalformed("usage batch is not canonical JSON") from exc
        if canonical_wire != encoded:
            raise UsageBatchMalformed("usage batch wire is not canonical JSON")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "UsageBatch":
        if not isinstance(raw, Mapping) or set(raw) != _WIRE_FIELDS:
            missing = sorted(_WIRE_FIELDS - set(raw)) if isinstance(raw, Mapping) else []
            extra = sorted(set(raw) - _WIRE_FIELDS) if isinstance(raw, Mapping) else []
            raise UsageBatchMalformed(
                f"usage batch fields do not match v1 (missing={missing}, extra={extra})"
            )
        if raw["v"] != USAGE_BATCH_VERSION or isinstance(raw["v"], bool):
            raise UsageBatchMalformed("unsupported usage batch version")
        values = cls._validated_values(
            producer=raw["producer"],
            organization_id=raw["organization_id"],
            sequence=raw["sequence"],
            interval_start=raw["interval_start"],
            interval_end=raw["interval_end"],
            created_at=raw["created_at"],
            counters=raw["counters"],
        )
        batch_id = raw["batch_id"]
        checksum = raw["checksum"]
        signature = raw["signature"]
        if not isinstance(batch_id, str) or _HEX_64_RE.fullmatch(batch_id) is None:
            raise UsageBatchMalformed("batch_id must be 64 lowercase hex chars")
        if not isinstance(checksum, str) or _HEX_64_RE.fullmatch(checksum) is None:
            raise UsageBatchMalformed("checksum must be 64 lowercase hex chars")
        if not isinstance(signature, str) or _HEX_128_RE.fullmatch(signature) is None:
            raise UsageBatchMalformed("signature must be 128 lowercase hex chars")

        identity = _identity_dict(
            **{
                key: values[key]
                for key in (
                    "producer",
                    "organization_id",
                    "sequence",
                    "interval_start",
                    "interval_end",
                )
            }
        )
        expected_id = _batch_id(identity)
        if batch_id != expected_id:
            raise UsageBatchMalformed("batch_id does not match its logical identity")
        unsigned = cls._unsigned_dict(batch_id=batch_id, **values)
        expected_checksum = _checksum(unsigned)
        if checksum != expected_checksum:
            raise UsageBatchMalformed("checksum does not match usage batch content")
        signed = {**unsigned, "checksum": checksum}
        try:
            verify_signature(
                values["producer"],
                signature,
                SIGNATURE_DOMAIN + canonical_json(signed),
            )
        except IdkitError as exc:
            raise UsageBatchSignatureError("usage batch signature does not verify") from exc
        return cls._construct(
            **values,
            batch_id=batch_id,
            checksum=checksum,
            signature=signature,
        )

    @classmethod
    def _validated_values(
        cls,
        *,
        producer: object,
        organization_id: object,
        sequence: object,
        interval_start: object,
        interval_end: object,
        created_at: object,
        counters: object,
    ) -> dict:
        producer_value = _producer_key(producer)
        org_value = _canonical_uuid(organization_id)
        sequence_value = _strict_int(sequence, "sequence", minimum=1)
        start_value = _strict_int(interval_start, "interval_start")
        end_value = _strict_int(interval_end, "interval_end")
        created_value = _strict_int(created_at, "created_at")
        if start_value % BATCH_INTERVAL_SECONDS != 0:
            raise UsageBatchMalformed("interval_start must be epoch-aligned")
        if end_value - start_value != BATCH_INTERVAL_SECONDS:
            raise UsageBatchMalformed(
                f"usage batches must span exactly {BATCH_INTERVAL_SECONDS} seconds"
            )
        if created_value < end_value:
            raise UsageBatchMalformed("created_at cannot precede interval_end")
        return {
            "producer": producer_value,
            "organization_id": org_value,
            "sequence": sequence_value,
            "interval_start": start_value,
            "interval_end": end_value,
            "created_at": created_value,
            "counters": _counter_map(counters),
        }

    @staticmethod
    def _unsigned_dict(
        *,
        batch_id: str,
        producer: str,
        organization_id: str,
        sequence: int,
        interval_start: int,
        interval_end: int,
        created_at: int,
        counters: Mapping[str, int],
    ) -> dict:
        return {
            "v": USAGE_BATCH_VERSION,
            "batch_id": batch_id,
            "producer": producer,
            "organization_id": organization_id,
            "sequence": sequence,
            "interval_start": interval_start,
            "interval_end": interval_end,
            "created_at": created_at,
            "counters": dict(counters),
        }

    @classmethod
    def _construct(cls, **values: object) -> "UsageBatch":
        values["counters"] = MappingProxyType(dict(values["counters"]))
        return cls(**values)  # type: ignore[arg-type]

    def to_dict(self) -> dict:
        unsigned = self._unsigned_dict(
            batch_id=self.batch_id,
            producer=self.producer,
            organization_id=self.organization_id,
            sequence=self.sequence,
            interval_start=self.interval_start,
            interval_end=self.interval_end,
            created_at=self.created_at,
            counters=self.counters,
        )
        return {**unsigned, "checksum": self.checksum, "signature": self.signature}

    def to_json(self) -> bytes:
        """Return the exact bytes a spool stores and retries."""
        return canonical_json(self.to_dict())
