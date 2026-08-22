"""Signed producer progress for settling idle-inclusive usage intervals."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Mapping

from tools.network.idkit import (
    KeyPair,
    canonical_json,
    load_public_key,
    verify_signature,
)
from tools.network.idkit.errors import IdkitError

from .batch import BATCH_INTERVAL_SECONDS, MAX_INT

USAGE_PROGRESS_VERSION = 1
MAX_PROGRESS_WIRE_BYTES = 4 * 1024
PROGRESS_ID_DOMAIN = b"autonomy.network.usage-progress-id.v1\n"
PROGRESS_CHECKSUM_DOMAIN = b"autonomy.network.usage-progress-checksum.v1\n"
PROGRESS_SIGNATURE_DOMAIN = b"autonomy.network.usage-progress-signature.v1\n"

_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_HEX_128_RE = re.compile(r"[0-9a-f]{128}")
_FIELDS = frozenset(
    {
        "v",
        "progress_id",
        "producer",
        "organization_id",
        "sequence",
        "closed_through",
        "created_at",
        "checksum",
        "signature",
    }
)


class UsageProgressError(ValueError):
    """Base class for producer-progress rejection."""


class UsageProgressMalformed(UsageProgressError):
    """The progress wire is not the one canonical v1 representation."""


class UsageProgressSignatureError(UsageProgressError):
    """The producer signature does not verify."""


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageProgressMalformed(f"{name} must be an integer")
    if value < 0 or value > MAX_INT:
        raise UsageProgressMalformed(f"{name} must be between 0 and {MAX_INT}")
    return value


def _org(value: object) -> str:
    if not isinstance(value, str):
        raise UsageProgressMalformed("organization_id must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise UsageProgressMalformed(
            "organization_id must be a canonical UUID"
        ) from exc
    if parsed.version is None or str(parsed) != value:
        raise UsageProgressMalformed("organization_id must be a canonical UUID")
    return value


def _producer(value: object) -> str:
    if not isinstance(value, str):
        raise UsageProgressMalformed("producer must be an Ed25519 public key")
    try:
        load_public_key(value)
    except IdkitError as exc:
        raise UsageProgressMalformed("producer must be an Ed25519 public key") from exc
    return value


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise UsageProgressMalformed(f"duplicate JSON field: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class UsageProgress:
    producer: str
    organization_id: str
    sequence: int
    closed_through: int
    created_at: int
    progress_id: str
    checksum: str
    signature: str

    @classmethod
    def create(
        cls,
        *,
        signer: KeyPair,
        organization_id: str,
        sequence: int,
        closed_through: int,
        created_at: int,
    ) -> "UsageProgress":
        values = cls._validated(
            producer=signer.public_hex,
            organization_id=organization_id,
            sequence=sequence,
            closed_through=closed_through,
            created_at=created_at,
        )
        identity = cls._identity(**values)
        progress_id = hashlib.sha256(
            PROGRESS_ID_DOMAIN + canonical_json(identity)
        ).hexdigest()
        unsigned = cls._unsigned(progress_id=progress_id, **values)
        checksum = hashlib.sha256(
            PROGRESS_CHECKSUM_DOMAIN + canonical_json(unsigned)
        ).hexdigest()
        signed = {**unsigned, "checksum": checksum}
        signature = signer.sign_hex(
            PROGRESS_SIGNATURE_DOMAIN + canonical_json(signed)
        )
        return cls(**values, progress_id=progress_id, checksum=checksum, signature=signature)

    @classmethod
    def from_json(cls, wire: bytes | str) -> "UsageProgress":
        if isinstance(wire, str):
            try:
                encoded = wire.encode("ascii")
            except UnicodeEncodeError as exc:
                raise UsageProgressMalformed("usage progress must be ASCII") from exc
        elif isinstance(wire, bytes):
            encoded = wire
        else:
            raise UsageProgressMalformed("usage progress must be bytes or string")
        if not encoded or len(encoded) > MAX_PROGRESS_WIRE_BYTES:
            raise UsageProgressMalformed("usage progress wire size is invalid")
        try:
            raw = json.loads(encoded.decode("ascii"), object_pairs_hook=_no_duplicates)
        except UsageProgressMalformed:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageProgressMalformed("usage progress is not valid JSON") from exc
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise UsageProgressMalformed("usage progress fields do not match v1")
        try:
            canonical = canonical_json(raw)
        except IdkitError as exc:
            raise UsageProgressMalformed("usage progress is not canonical JSON") from exc
        if canonical != encoded:
            raise UsageProgressMalformed("usage progress wire is not canonical JSON")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "UsageProgress":
        if not isinstance(raw, Mapping) or set(raw) != _FIELDS:
            raise UsageProgressMalformed("usage progress fields do not match v1")
        if raw["v"] != USAGE_PROGRESS_VERSION or isinstance(raw["v"], bool):
            raise UsageProgressMalformed("unsupported usage progress version")
        values = cls._validated(
            producer=raw["producer"],
            organization_id=raw["organization_id"],
            sequence=raw["sequence"],
            closed_through=raw["closed_through"],
            created_at=raw["created_at"],
        )
        progress_id, checksum, signature = (
            raw["progress_id"],
            raw["checksum"],
            raw["signature"],
        )
        if not isinstance(progress_id, str) or not _HEX_64_RE.fullmatch(progress_id):
            raise UsageProgressMalformed("progress_id must be lowercase hex")
        if not isinstance(checksum, str) or not _HEX_64_RE.fullmatch(checksum):
            raise UsageProgressMalformed("checksum must be lowercase hex")
        if not isinstance(signature, str) or not _HEX_128_RE.fullmatch(signature):
            raise UsageProgressMalformed("signature must be lowercase hex")
        expected_id = hashlib.sha256(
            PROGRESS_ID_DOMAIN + canonical_json(cls._identity(**values))
        ).hexdigest()
        if progress_id != expected_id:
            raise UsageProgressMalformed("progress_id does not match content")
        unsigned = cls._unsigned(progress_id=progress_id, **values)
        expected_checksum = hashlib.sha256(
            PROGRESS_CHECKSUM_DOMAIN + canonical_json(unsigned)
        ).hexdigest()
        if checksum != expected_checksum:
            raise UsageProgressMalformed("checksum does not match content")
        signed = {**unsigned, "checksum": checksum}
        try:
            verify_signature(
                values["producer"],
                signature,
                PROGRESS_SIGNATURE_DOMAIN + canonical_json(signed),
            )
        except IdkitError as exc:
            raise UsageProgressSignatureError(
                "usage progress signature does not verify"
            ) from exc
        return cls(
            **values,
            progress_id=progress_id,
            checksum=checksum,
            signature=signature,
        )

    @classmethod
    def _validated(cls, **raw: object) -> dict:
        values = {
            "producer": _producer(raw["producer"]),
            "organization_id": _org(raw["organization_id"]),
            "sequence": _integer(raw["sequence"], "sequence"),
            "closed_through": _integer(raw["closed_through"], "closed_through"),
            "created_at": _integer(raw["created_at"], "created_at"),
        }
        if values["closed_through"] % BATCH_INTERVAL_SECONDS:
            raise UsageProgressMalformed("closed_through must be epoch-aligned")
        if values["created_at"] < values["closed_through"]:
            raise UsageProgressMalformed("created_at cannot precede closed_through")
        return values

    @staticmethod
    def _identity(**values: object) -> dict:
        return {
            "v": USAGE_PROGRESS_VERSION,
            "producer": values["producer"],
            "organization_id": values["organization_id"],
            "sequence": values["sequence"],
            "closed_through": values["closed_through"],
        }

    @staticmethod
    def _unsigned(*, progress_id: str, **values: object) -> dict:
        return {
            **UsageProgress._identity(**values),
            "progress_id": progress_id,
            "created_at": values["created_at"],
        }

    def to_dict(self) -> dict:
        unsigned = self._unsigned(
            progress_id=self.progress_id,
            producer=self.producer,
            organization_id=self.organization_id,
            sequence=self.sequence,
            closed_through=self.closed_through,
            created_at=self.created_at,
        )
        return {**unsigned, "checksum": self.checksum, "signature": self.signature}

    def to_json(self) -> bytes:
        return canonical_json(self.to_dict())
