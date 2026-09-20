"""``autonomy.machine.serve-cert#1`` — THIS machine's serving credential for
one organization: the certificates, readable while the vault is cold, and the
name of the machine-vault row that holds the key.

Design of record: graph://67d0aa5f-885 (D5, corrected 2026-09-20, comment
e88699ed-fc5). Supersedes ``autonomy.network.serve-cert`` for every reader.

Why machine-homed: a serving credential is minted per machine at sign-on and
is used by this machine's connector alone. The organization-homed row it
replaces replicated every member's certificates to every other member and
named a local key FILE by basename; a single shared row once made every
other machine read ``key-missing`` and made each mint evict the last working
machine (graph://90ba11c8-3d3). Nobody outside this machine needs the row:
the relay receives the certificate inside the tunnel hello, and the
supervisor reads it locally.

Why plain, not vaulted: status surfaces (the Fleet card, the profile tray,
the pre-unlock "does this org need a fresh credential" answer) must work
while the vault is cold. The private key is the only secret and it lives in
``autonomy.machine.vault.audited`` under ``vault_key``; the row here holds no
key material and no path.

Key: the organization's registry ``org_uuid`` — one machine serves several
organizations and holds one credential per organization.

Two certificate shapes exist and both validate here with the shared checks
of the organization-homed revisions: PERSONA-signed (``persona_pub``; the
collaborative-org self-service credential) and ROOT-signed (``root_pub``
with a ``viewer_cert``; the personal organization's bootstrap credential).
Exactly one anchor is present.
"""
from __future__ import annotations

from typing import Any

from .network_identity import (
    NETWORK_PUB_HEX_LEN,
    _require_hex,
    _require_str,
    _validate_persona_serve_cert,
    _validate_root_serve_cert,
)
from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

MACHINE_SERVE_CERT_SET_ID = "autonomy.machine.serve-cert"
MACHINE_SERVE_CERT_REVISION = 1

SYNOPSIS = {
    "summary": (
        "This machine's serving credential for one organization: the "
        "tunnel:serve certificate(s), their validity, and the machine-vault "
        "row name of the serving key. Machine-homed, plain, keyed by org_uuid."
    ),
    "nouns": [
        "serve cert", "serving credential", "serving delegate", "tunnel:serve",
        "serving key", "connector credential",
    ],
    "related_set_ids": [
        "autonomy.machine.vault.audited#1",
        "autonomy.network.binding#2",
    ],
}


def serving_key_vault_key(org_uuid: str, child_pub: str) -> str:
    """The machine-vault row that holds the serving key for *org_uuid*:
    ``serving-key.<org_uuid>.<child_pub>``. The child key is part of the name
    so that replacing a credential is transactional: the new key is sealed
    under its own row before the serve-cert row is switched to it, and a
    failed row write leaves the previous row still naming its own,
    still-present key. The previous key's row is removed after the switch."""
    return f"serving-key.{org_uuid}.{child_pub}"


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="org_uuid")
class MachineServeCertV1(SettingSchema):
    """One organization's serving credential on this machine; see the module."""

    set_id = MACHINE_SERVE_CERT_SET_ID
    schema_revision = MACHINE_SERVE_CERT_REVISION

    cert: str = field(
        required=True,
        description=(
            "The tunnel:serve delegation certificate in canonical idkit wire "
            "JSON, used for registry tunnel admission."
        ),
    )
    viewer_cert: str | None = field(
        required=False,
        description=(
            "ROOT-signed shape only: the identity-neutral certificate over the "
            "same child for viewer SERVER_HELLO bytes. Absent for the "
            "persona-signed shape (viewers verify the per-link channel key)."
        ),
    )
    dns01_cert: str | None = field(
        required=False,
        description=(
            "Optional serve:dns-01 certificate over the same serving child, "
            "same subject, same validity window."
        ),
    )
    persona_pub: str | None = field(
        required=False,
        description="PERSONA-signed shape: the persona the chain anchors at.",
    )
    root_pub: str | None = field(
        required=False,
        description="ROOT-signed shape: the organization root the chain anchors at.",
    )
    not_after: int = field(
        required=True,
        description="Epoch seconds the delegate expires (mirrors the cert).",
    )
    child_pub: str = field(
        required=True,
        description=(
            "The serving child's public key (64 lowercase hex); equals the "
            "cert's child_pub and identifies the connector's working files."
        ),
    )
    vault_key: str = field(
        required=True,
        description=(
            "The autonomy.machine.vault.audited row holding this child's "
            "private key: serving-key.<org_uuid>.<child_pub>. Never a "
            "filesystem path."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        if "key_path" in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: key_path is retired — the serving key lives in "
                "the machine vault (graph://67d0aa5f-885 D4), never in a file"
            )
        has_persona = payload.get("persona_pub") is not None
        has_root = payload.get("root_pub") is not None
        if has_persona == has_root:
            raise SchemaValidationError(
                f"{cls.__name__}: exactly one of persona_pub (persona-signed) or "
                "root_pub (root-signed) must be present"
            )
        child_pub = _require_hex(payload, "child_pub", cls.__name__, length=NETWORK_PUB_HEX_LEN)
        vault_key = _require_str(payload, "vault_key", cls.__name__, max_len=256)
        if not vault_key.startswith("serving-key.") or "/" in vault_key \
                or not vault_key.endswith("." + child_pub):
            raise SchemaValidationError(
                f"{cls.__name__}: vault_key must name the "
                "serving-key.<org_uuid>.<child_pub> row of the machine vault for "
                "this child"
            )
        if has_persona:
            _validate_persona_serve_cert(payload, cls.__name__, require_key_path=False)
        else:
            _validate_root_serve_cert(payload, cls.__name__, require_key_path=False)
        # The shared checks parsed the cert; bind child_pub to it.
        from tools.network.idkit import DelegationCert

        if DelegationCert.from_json(payload["cert"]).child_pub != child_pub:
            raise SchemaValidationError(
                f"{cls.__name__}: child_pub does not match the cert's child_pub"
            )
