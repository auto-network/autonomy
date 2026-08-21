"""``autonomy.org.capability.install#1`` — org-level capability binding.

An *org install* records the org's choice of which
:mod:`tools.graph.schemas.capability_impl` it uses for a given
:mod:`tools.graph.schemas.capability_contract`, plus the org-level
defaults the runtime needs to materialize that implementation:

* env var bindings (env name -> source identifier)
* secret-file bindings (container path -> source identifier)
* mount bindings (container path -> source identifier)
* broker config (non-secret settings a host-side broker resolves at call
  time — e.g. the Jira base URL, account email, and token-file PATH; the
  secret itself stays in the host file, never in the graph)

This schema does not materialize anything by itself. It declares what the
workspace launcher materializes when a workspace enables the contract; the
launcher and readiness checker share the same versioned-chain validator (see
graph://86e04207-a25 § Runtime materialization).

Versioning semantics: identifiers in ``contract`` and ``implementation``
are unpinned strings; the integer ``contract_version`` and
``implementation_version`` fields pin to a specific canonical version. A
``@N`` suffix on the *string* is not used here — versioning is carried
explicitly by the integer fields. The release/pin workflow is out of
scope for this bead.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    RemediationRef,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)
from .registry import home


SET_ID = "autonomy.org.capability.install"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Org-level capability binding: which implementation the org runs "
        "for a given contract, plus env/secret/mount bindings"
    ),
    "nouns": [
        "capability", "install", "binding", "org install",
        "env binding", "secret binding", "mount binding",
    ],
    "related_set_ids": [
        "autonomy.capability.contract#1",
        "autonomy.capability.impl#1",
        "autonomy.workspace.capability.enable#1",
    ],
}


_ALLOWED_TOP_LEVEL = {
    "contract",
    "contract_version",
    "implementation",
    "implementation_version",
    "env_bindings",
    "secret_file_bindings",
    "mount_bindings",
    "broker_config",
    "notes",
}

_BINDING_FIELDS = (
    "env_bindings", "secret_file_bindings", "mount_bindings", "broker_config",
)


def _validate_str_str_map(payload: dict, key: str, cls_name: str) -> None:
    if key not in payload:
        return
    val = payload[key]
    if not isinstance(val, dict):
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be an object, got {type(val).__name__}"
        )
    for k, v in val.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise SchemaValidationError(
                f"{cls_name}: {key!r} must map str -> str; "
                f"bad entry {k!r}={v!r}"
            )


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@keyed_per_entity(key_strategy="contract_name")
class OrgCapabilityInstallV1(SettingSchema):
    """Shape of an ``autonomy.org.capability.install#1`` Setting payload.

    Required: ``contract`` (string), ``contract_version`` (int),
    ``implementation`` (string), ``implementation_version`` (int).

    Optional: ``env_bindings``, ``secret_file_bindings``,
    ``mount_bindings`` (each a string -> string mapping when present);
    ``notes`` (string).

    Migrated to the typed-``field()`` declaration shape (auto-4n966 /
    Bead 1A) — annotations now drive ``_field_metadata`` derivation
    via ``__init_subclass__``. Imperative validation in
    :meth:`validate` continues to enforce shape checks the typed
    metadata can't yet describe (str -> str maps, integer minimums,
    extra-field rejection).
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    contract: str = field(
        required=True,
        description="Contract identifier (unpinned name; version is the integer field)",
        references="autonomy.capability.contract",
    )
    contract_version: int = field(
        required=True,
        description="Pinned canonical contract version (>= 1)",
    )
    implementation: str = field(
        required=True,
        description="Implementation identifier (e.g. autonomy/github)",
        references="autonomy.capability.impl",
    )
    implementation_version: int = field(
        required=True,
        description="Pinned canonical implementation version (>= 1)",
    )
    env_bindings: dict = field(
        required=False,
        description="Env var bindings: env name -> source identifier",
        remediation=RemediationRef("capability.env-binding.v1"),
    )
    secret_file_bindings: dict = field(
        required=False,
        description="Secret-file bindings: container path -> source identifier",
    )
    mount_bindings: dict = field(
        required=False,
        description="Mount bindings: container path -> source identifier",
    )
    broker_config: dict = field(
        required=False,
        description=(
            "Non-secret host-broker settings (str -> str), e.g. base_url, "
            "email, token_file PATH — never a literal secret"
        ),
    )
    notes: str = field(
        required=False,
        description="Free-form notes",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:  # noqa: C901 — flat checks
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )

        extra = set(payload) - _ALLOWED_TOP_LEVEL
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )

        for key in ("contract", "implementation"):
            val = payload.get(key)
            if not isinstance(val, str) or not val:
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} is required and must be a "
                    f"non-empty string"
                )

        for key in ("contract_version", "implementation_version"):
            val = payload.get(key)
            if isinstance(val, bool) or not isinstance(val, int):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} is required and must be an "
                    f"integer"
                )
            if val < 1:
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be >= 1, got {val}"
                )

        for key in _BINDING_FIELDS:
            _validate_str_str_map(payload, key, cls.__name__)

        if "notes" in payload and not isinstance(payload["notes"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'notes' must be a string"
            )
