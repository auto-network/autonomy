"""Machine-local public metadata for persona Service TLS certificates."""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


SERVICE_CERTIFICATE_SET_ID = "autonomy.network.service-certificate"
SERVICE_CERTIFICATE_REVISION = 1

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SERIAL_RE = re.compile(r"^[0-9a-f]+$")


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="certificate_identity")
class ServiceCertificateV1(SettingSchema):
    """The active verified public certificate and its opaque vault locator."""

    set_id = SERVICE_CERTIFICATE_SET_ID
    schema_revision = SERVICE_CERTIFICATE_REVISION

    org: str = field(required=True, description="Owning organization slug.")
    persona_label: str = field(required=True, description="Serving persona DNS label.")
    apex: str = field(required=True, description="Persona serving apex hostname.")
    sans: list = field(required=True, description="Exact apex and wildcard SAN pair.")
    not_before: int = field(required=True, description="Certificate start epoch seconds.")
    not_after: int = field(required=True, description="Certificate expiry epoch seconds.")
    serial: str = field(required=True, description="Lowercase hexadecimal serial.")
    vault_key: str = field(required=True, description="Audited-vault bundle key.")
    staging: bool = field(required=True, description="Whether the issuing CA was staging.")
    activated_at: int = field(required=True, description="Activation epoch seconds.")
    previous_serial: str = field(required=False, description="Immediate rollback generation.")

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        if not isinstance(key, str) or key.count(":") != 1:
            raise SchemaValidationError(
                f"{cls.__name__}: key must be '<org>:<persona_label>'"
            )
        org, persona = key.split(":", 1)
        if not _LABEL_RE.fullmatch(org) or not _LABEL_RE.fullmatch(persona):
            raise SchemaValidationError(
                f"{cls.__name__}: key has an invalid organization or persona label"
            )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        org = payload.get("org")
        persona = payload.get("persona_label")
        if not isinstance(org, str) or not _LABEL_RE.fullmatch(org):
            raise SchemaValidationError(f"{cls.__name__}: org is invalid")
        if not isinstance(persona, str) or not _LABEL_RE.fullmatch(persona):
            raise SchemaValidationError(f"{cls.__name__}: persona_label is invalid")
        apex = f"{persona}.serve.auto.network"
        if payload.get("apex") != apex:
            raise SchemaValidationError(f"{cls.__name__}: apex does not match persona")
        if payload.get("sans") != [f"*.{apex}", apex]:
            raise SchemaValidationError(f"{cls.__name__}: SANs are not the exact pair")
        for name in ("not_before", "not_after", "activated_at"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SchemaValidationError(f"{cls.__name__}: {name} must be positive")
        if payload["not_before"] >= payload["not_after"]:
            raise SchemaValidationError(f"{cls.__name__}: validity interval is empty")
        serial = payload.get("serial")
        if not isinstance(serial, str) or not _SERIAL_RE.fullmatch(serial):
            raise SchemaValidationError(f"{cls.__name__}: serial is invalid")
        previous = payload.get("previous_serial")
        if previous is not None and (
            not isinstance(previous, str) or not _SERIAL_RE.fullmatch(previous)
        ):
            raise SchemaValidationError(f"{cls.__name__}: previous_serial is invalid")
        vault_key = payload.get("vault_key")
        if not isinstance(vault_key, str) or not vault_key:
            raise SchemaValidationError(f"{cls.__name__}: vault_key is required")


SYNOPSIS = {
    "summary": "Machine-local active metadata for verified persona wildcard certificates.",
    "nouns": ["Service certificate", "persona wildcard TLS", "certificate rotation"],
    "related_set_ids": [
        "autonomy.network.namespace-reservation#1",
        "autonomy.vault.audited#1",
    ],
}
