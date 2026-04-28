"""``autonomy.org.capability.install#1`` — org-level capability binding.

An *org install* records the org's choice of which
:mod:`tools.graph.schemas.capability_impl` it uses for a given
:mod:`tools.graph.schemas.capability_contract`, plus the org-level
defaults the runtime needs to materialize that implementation:

* env var bindings (env name -> source identifier)
* secret-file bindings (container path -> source identifier)
* mount bindings (container path -> source identifier)

This schema does **not** materialize anything in a workspace. It only
declares what the org *would* materialize when a workspace enables the
contract. The actual materialization runs at workspace launch through a
later bead (see graph://86e04207-a25 § Runtime materialization).

Versioning semantics: identifiers in ``contract`` and ``implementation``
are unpinned strings; the integer ``contract_version`` and
``implementation_version`` fields pin to a specific canonical version. A
``@N`` suffix on the *string* is not used here — versioning is carried
explicitly by the integer fields. The release/pin workflow is out of
scope for this bead.
"""

from __future__ import annotations

from typing import Any

from .registry import SchemaValidationError, SettingSchema, register_schema


SET_ID = "autonomy.org.capability.install"
SCHEMA_REVISION = 1


_ALLOWED_TOP_LEVEL = {
    "contract",
    "contract_version",
    "implementation",
    "implementation_version",
    "env_bindings",
    "secret_file_bindings",
    "mount_bindings",
    "notes",
}

_BINDING_FIELDS = ("env_bindings", "secret_file_bindings", "mount_bindings")


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


class OrgCapabilityInstallV1(SettingSchema):
    """Shape of an ``autonomy.org.capability.install#1`` Setting payload.

    Required: ``contract`` (string), ``contract_version`` (int),
    ``implementation`` (string), ``implementation_version`` (int).

    Optional: ``env_bindings``, ``secret_file_bindings``,
    ``mount_bindings`` (each a string -> string mapping when present);
    ``notes`` (string).
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

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


register_schema(SET_ID, SCHEMA_REVISION, OrgCapabilityInstallV1)
