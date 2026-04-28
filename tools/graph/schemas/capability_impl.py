"""``autonomy.capability.impl#1`` — concrete implementation of capabilities.

A *capability implementation* binds one or more
:mod:`tools.graph.schemas.capability_contract` definitions to a concrete
adapter. It carries the runtime delivery details (image-baked binaries,
mounted tool bundles, host proxy, hybrid), the package root holding the
implementation's tools/skill/primer, and probe info that lets the runtime
verify the capability is actually usable.

This schema does **not** materialize anything into a workspace. Runtime
materialization is the job of later beads — see graph://86e04207-a25
§ "Runtime materialization and probe model".

Versioning semantics (mirrors the contract schema):

* ``version`` is a single monotonic integer; ``name@1`` is the pinned
  canonical revision, ``name`` is the unpinned working version.
* Edits within the working version do **not** advance ``version``; only
  an explicit RELEASE/PIN does. The release workflow itself is out of
  scope for this bead.

Each entry in ``implements`` references a contract by ``contract`` name
and ``version``. An implementation may declare it implements multiple
contracts (e.g. ``autonomy/github`` implements ``source_control@1``,
``change_review@1``, and ``merge_gates@1``).
"""

from __future__ import annotations

from typing import Any

from .registry import SchemaValidationError, SettingSchema, register_schema


SET_ID = "autonomy.capability.impl"
SCHEMA_REVISION = 1


VALID_DELIVERY_MODES = ("image_baked", "mounted_tools", "host_proxy", "hybrid")


_ALLOWED_TOP_LEVEL = {
    "name",
    "version",
    "implements",
    "delivery_mode",
    "package_root",
    "probe",
    "required_env",
    "required_secret_files",
    "tool_paths",
    "skill_path",
    "primer_path",
    "notes",
}

_CONTRACT_REF_REQUIRED = ("contract", "version")
_CONTRACT_REF_ALLOWED = set(_CONTRACT_REF_REQUIRED)

_PROBE_REQUIRED = ("kind", "entrypoint")


def _validate_contract_ref(ref: Any, idx: int, cls_name: str) -> None:
    if not isinstance(ref, dict):
        raise SchemaValidationError(
            f"{cls_name}: implements[{idx}] must be a mapping, "
            f"got {type(ref).__name__}"
        )
    extra = set(ref) - _CONTRACT_REF_ALLOWED
    if extra:
        raise SchemaValidationError(
            f"{cls_name}: implements[{idx}] has unknown field(s): "
            f"{sorted(extra)}"
        )
    for key in _CONTRACT_REF_REQUIRED:
        if key not in ref:
            raise SchemaValidationError(
                f"{cls_name}: implements[{idx}] missing required field "
                f"{key!r}"
            )
    contract = ref["contract"]
    if not isinstance(contract, str) or not contract:
        raise SchemaValidationError(
            f"{cls_name}: implements[{idx}].contract must be a "
            f"non-empty string"
        )
    version = ref["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise SchemaValidationError(
            f"{cls_name}: implements[{idx}].version must be an integer"
        )
    if version < 1:
        raise SchemaValidationError(
            f"{cls_name}: implements[{idx}].version must be >= 1, "
            f"got {version}"
        )


def _validate_probe(probe: Any, cls_name: str) -> None:
    if not isinstance(probe, dict):
        raise SchemaValidationError(
            f"{cls_name}: 'probe' must be an object, "
            f"got {type(probe).__name__}"
        )
    for key in _PROBE_REQUIRED:
        if key not in probe:
            raise SchemaValidationError(
                f"{cls_name}: 'probe' missing required field {key!r}"
            )
        val = probe[key]
        if not isinstance(val, str) or not val:
            raise SchemaValidationError(
                f"{cls_name}: 'probe.{key}' must be a non-empty string"
            )


def _validate_str_list(payload: dict, key: str, cls_name: str) -> None:
    if key not in payload:
        return
    val = payload[key]
    if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
        raise SchemaValidationError(
            f"{cls_name}: {key!r} must be a list of strings"
        )


class CapabilityImplV1(SettingSchema):
    """Shape of an ``autonomy.capability.impl#1`` Setting payload.

    Required: ``name``, ``version`` (int >= 1), ``implements`` (non-empty
    list of contract refs), ``delivery_mode`` (one of
    :data:`VALID_DELIVERY_MODES`), ``package_root`` (repo-local path),
    ``probe`` (object with ``kind`` and ``entrypoint``).

    Optional: ``required_env``, ``required_secret_files``, ``tool_paths``,
    ``skill_path``, ``primer_path``, ``notes``.
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

        # name
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise SchemaValidationError(
                f"{cls.__name__}: 'name' is required and must be a "
                f"non-empty string"
            )

        # version
        version = payload.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise SchemaValidationError(
                f"{cls.__name__}: 'version' is required and must be an integer"
            )
        if version < 1:
            raise SchemaValidationError(
                f"{cls.__name__}: 'version' must be >= 1, got {version}"
            )

        # implements
        implements = payload.get("implements")
        if not isinstance(implements, list) or not implements:
            raise SchemaValidationError(
                f"{cls.__name__}: 'implements' is required and must be a "
                f"non-empty list of contract refs"
            )
        for i, ref in enumerate(implements):
            _validate_contract_ref(ref, i, cls.__name__)

        # delivery_mode
        delivery_mode = payload.get("delivery_mode")
        if delivery_mode not in VALID_DELIVERY_MODES:
            raise SchemaValidationError(
                f"{cls.__name__}: 'delivery_mode' must be one of "
                f"{VALID_DELIVERY_MODES}, got {delivery_mode!r}"
            )

        # package_root
        package_root = payload.get("package_root")
        if not isinstance(package_root, str) or not package_root:
            raise SchemaValidationError(
                f"{cls.__name__}: 'package_root' is required and must be a "
                f"non-empty string"
            )
        if package_root.startswith("/"):
            raise SchemaValidationError(
                f"{cls.__name__}: 'package_root' must be repo-local "
                f"(not absolute), got {package_root!r}"
            )

        # probe
        if "probe" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'probe' is required"
            )
        _validate_probe(payload["probe"], cls.__name__)

        # Optional string-list fields.
        _validate_str_list(payload, "required_env", cls.__name__)
        _validate_str_list(payload, "required_secret_files", cls.__name__)
        _validate_str_list(payload, "tool_paths", cls.__name__)

        # Optional string fields.
        for key in ("skill_path", "primer_path", "notes"):
            if key in payload and not isinstance(payload[key], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a string"
                )


register_schema(SET_ID, SCHEMA_REVISION, CapabilityImplV1)
