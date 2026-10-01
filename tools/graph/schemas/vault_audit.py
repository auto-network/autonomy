"""``autonomy.vault.audit`` — the vault audit table: one row per delivered
vault secret.

Every delivery of a vault secret to a session writes one row: which secret
(``setting_name``), which session, where the copy was placed
(``container_path`` / ``host_path``), when (``delivered_at``), its deadline
(``expires_at``), and -- once the copy is destroyed -- when and why
(``shredded_at`` / ``shred_reason``). The row is written BEFORE the delivery
layer materialises the ramfs file, so the only crash state is a row with no
file, never a file with no row. It never holds plaintext, a sealed key, or
ciphertext: a reader learns which secret went to which session and when its
copy was destroyed, not what it was.

Machine-homed by ruling (mission decision ``d-no-bespoke-stores``): every
machine-local operational record is a declared Setting schema in the machine
store, never a bespoke database file. Rows are updated in place when the
copy is destroyed, never deleted, so the audit outlives the secret.
"""

from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)

VAULT_AUDIT_SET_ID = "autonomy.vault.audit"

SYNOPSIS = {
    "summary": (
        "Machine-homed vault audit table: one row per delivered vault secret "
        "(which secret, which session, deadline, when the copy was destroyed), "
        "written before the ramfs file exists — never plaintext or key material."
    ),
    "nouns": [
        "vault audit", "delivered secret", "vault delivery",
        "secret access record", "copy destruction",
    ],
    "related_set_ids": ["autonomy.vault.secured#1"],
}

VAULT_AUDIT_REVISION = 1

#: Closed vocabulary so the audit language cannot drift. ``expired`` = the
#: deadline passed; ``session_end`` = the session's subdirectory was
#: reclaimed; ``reconciled`` = destroyed by start-up reconciliation;
#: ``orphaned`` = the session no longer exists; ``delivery_failed`` = the
#: record committed but the ramfs file never materialised.
SHRED_REASONS = frozenset({
    "expired", "session_end", "reconciled", "orphaned", "delivery_failed",
})

_REQUIRED_STR = ("session", "setting_name", "release_mode",
                 "container_path", "host_path")
_INT_FIELDS = ("delivered_at",)
_OPTIONAL_INT_FIELDS = ("expires_at",)


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="release_id")
class VaultAuditV1(SettingSchema):
    set_id = VAULT_AUDIT_SET_ID
    schema_revision = VAULT_AUDIT_REVISION

    session: str = field(required=True, description="Requesting session the file was delivered to.")
    setting_name: str = field(required=True, description="The released Setting's name; never its value.")
    release_mode: str = field(required=True, description="Always 'delivered' — the only mode that leaves a file.")
    delivered_at: int = field(required=True, description="Unix ms the record committed (before materialisation).")
    expires_at: int = field(
        required=False,
        description=(
            "Unix ms deadline after which the sweeper destroys the file. "
            "Absent means SESSION LIFETIME: the artifact is destroyed at "
            "session end (or by the orphan sweep), never by deadline — the "
            "shape of a credential file the session uses for as long as it "
            "runs."
        ),
    )
    container_path: str = field(required=True, description="The file as the session sees it (audit only).")
    host_path: str = field(required=True, description="The file on the host, where the sweeper unlinks.")
    shredded_at: int = field(required=False, description="Unix ms the file was destroyed; absent while outstanding.")
    shred_reason: str = field(required=False, description="Why the file was destroyed, from the closed vocabulary.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        known = (
            set(_REQUIRED_STR) | set(_INT_FIELDS) | set(_OPTIONAL_INT_FIELDS)
            | {"shredded_at", "shred_reason"}
        )
        extra = set(payload) - known
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown fields {sorted(extra)!r}"
            )
        for name in _REQUIRED_STR:
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise SchemaValidationError(
                    f"{cls.__name__}: {name} must be a non-empty string"
                )
        if payload["release_mode"] != "delivered":
            raise SchemaValidationError(
                f"{cls.__name__}: release_mode must be 'delivered' — no other "
                "mode leaves a host artifact to sweep"
            )
        for name in _INT_FIELDS:
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SchemaValidationError(
                    f"{cls.__name__}: {name} must be an integer (unix ms)"
                )
        for name in _OPTIONAL_INT_FIELDS:
            if name not in payload:
                continue
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SchemaValidationError(
                    f"{cls.__name__}: {name} must be an integer (unix ms) "
                    f"when present"
                )
        shredded = payload.get("shredded_at")
        reason = payload.get("shred_reason")
        if (shredded is None) != (reason is None):
            raise SchemaValidationError(
                f"{cls.__name__}: shredded_at and shred_reason are set together"
            )
        if shredded is not None:
            if isinstance(shredded, bool) or not isinstance(shredded, int):
                raise SchemaValidationError(
                    f"{cls.__name__}: shredded_at must be an integer (unix ms)"
                )
            if reason not in SHRED_REASONS:
                raise SchemaValidationError(
                    f"{cls.__name__}: shred_reason {reason!r} not in "
                    f"{sorted(SHRED_REASONS)}"
                )
