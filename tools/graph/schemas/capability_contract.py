"""``autonomy.capability.contract#1`` — provider-agnostic capability contract.

A *capability contract* is the deterministic, provider-agnostic interface
that Dashboard, Worktrees, probes, and other automation rely on. It does
not commit to any particular vendor — concrete implementations
(``autonomy/github``, ``autonomy/jira``, ...) are declared separately by
:mod:`tools.graph.schemas.capability_impl`.

A contract groups one or more named operations. Each operation declares
JSON-shaped input/output schemas so consumers can typecheck their calls
without binding to a specific provider.

Initial contract families for the v1 model (see graph://86e04207-a25):

* ``issue_tracker`` — read/write tickets (Jira, Linear, GitHub Issues, ...)
* ``source_control`` — branch and commit state, with review and
  merge-gate concerns nested under ``source_control@1`` rather than
  split into separate top-level contracts. (The aspirational note
  sketches a future split into ``change_review`` and ``merge_gates``;
  the v1 placeholder schemas do not encode them as top-level contract
  families.) The full nested op inventory for ``source_control@1`` is
  finalized in a later bead.

Versioning semantics (mirrors graph note discussion in this session):

* The public identifier carries a single monotonic integer ``version``.
* ``name@1`` denotes the *pinned canonical* version 1.
* ``name`` with no explicit version refers to the current *unpinned* /
  mutable working version.
* Edits within the current working version do **not** advance the public
  ``version`` number — the version only moves on an explicit RELEASE/PIN.
* The release/pin workflow itself is out of scope for this bead; this
  module only encodes the field shape and validation rules so later beads
  cannot drift from the agreed semantics.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import SchemaValidationError, SettingSchema, field, keyed_per_entity


SET_ID = "autonomy.capability.contract"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Provider-agnostic capability contract: deterministic interface "
        "(named ops with input/output schemas) consumers can rely on"
    ),
    "nouns": [
        "capability", "contract", "interface", "ops", "operations",
        "issue tracker", "source control",
    ],
    "related_set_ids": [
        "autonomy.capability.impl#1",
        "autonomy.org.capability.install#1",
        "autonomy.workspace.capability.enable#1",
    ],
}


_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_ALLOWED_TOP_LEVEL = {
    "name",
    "version",
    "summary",
    "ops",
    "notes",
    "ui_hints",
}

_OP_REQUIRED = ("name", "summary", "input_schema", "output_schema")
_OP_ALLOWED = set(_OP_REQUIRED)


def _validate_op(op: Any, idx: int, cls_name: str) -> str:
    if not isinstance(op, dict):
        raise SchemaValidationError(
            f"{cls_name}: ops[{idx}] must be a mapping, "
            f"got {type(op).__name__}"
        )
    extra = set(op) - _OP_ALLOWED
    if extra:
        raise SchemaValidationError(
            f"{cls_name}: ops[{idx}] has unknown field(s): {sorted(extra)}"
        )
    for key in _OP_REQUIRED:
        if key not in op:
            raise SchemaValidationError(
                f"{cls_name}: ops[{idx}] missing required field {key!r}"
            )
    name = op["name"]
    if not isinstance(name, str) or not name:
        raise SchemaValidationError(
            f"{cls_name}: ops[{idx}].name must be a non-empty string"
        )
    if not _SNAKE_CASE_RE.match(name):
        raise SchemaValidationError(
            f"{cls_name}: ops[{idx}].name must be lowercase snake_case, "
            f"got {name!r}"
        )
    summary = op["summary"]
    if not isinstance(summary, str) or not summary:
        raise SchemaValidationError(
            f"{cls_name}: ops[{idx}].summary must be a non-empty string"
        )
    for key in ("input_schema", "output_schema"):
        if not isinstance(op[key], dict):
            raise SchemaValidationError(
                f"{cls_name}: ops[{idx}].{key} must be an object, "
                f"got {type(op[key]).__name__}"
            )
    return name


@keyed_per_entity(key_strategy="contract_name")
class CapabilityContractV1(SettingSchema):
    """Shape of an ``autonomy.capability.contract#1`` Setting payload.

    Required: ``name`` (lowercase snake_case), ``version`` (int >= 1),
    ``summary``, ``ops`` (non-empty list of operation descriptors).

    Optional: ``notes``, ``ui_hints``.

    Migrated to the typed-``field()`` declaration shape (Bead 3C) —
    annotations now drive ``_field_metadata`` derivation via
    ``__init_subclass__``. Imperative validation in :meth:`validate`
    continues to enforce shape checks the typed metadata can't yet
    describe (snake_case names, op sub-shape, integer minimums,
    duplicate-op rejection, extra-field rejection).

    No variant subclasses are appropriate at this level: variants share
    ``set_id`` + ``schema_revision``, but ``capability_contract#1`` is
    the meta-shape every contract Setting must fit, and contract
    families (``issue_tracker``, ``source_control``, ...) are encoded
    as payload ``name`` values rather than pre-enumerated subschemas.
    The substrate's nested-namespace tree is exercised for hypothetical
    op trees in
    :func:`tools.graph.tests.test_schemas_registry_variants.test_capability_layer_nested_namespace_shape`
    and is unaffected by this migration.
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    name: str = field(
        required=True,
        description="Lowercase snake_case identifier for the contract family",
    )
    version: int = field(
        required=True,
        description=(
            "Single monotonic integer version. ``name@N`` denotes the "
            "pinned canonical revision."
        ),
    )
    summary: str = field(
        required=True,
        description="Operator-facing one-line description of the contract",
    )
    ops: list = field(
        required=True,
        description="Named operations this contract groups",
        element={
            "name": {"type": "string", "required": True,
                     "description": "Op name (lowercase snake_case)"},
            "summary": {"type": "string", "required": True,
                        "description": "One-line op description"},
            "input_schema": {"type": "object", "required": True,
                             "description": "JSON-schema for op input"},
            "output_schema": {"type": "object", "required": True,
                              "description": "JSON-schema for op output"},
        },
    )
    notes: str = field(
        required=False,
        description="Free-form notes (design rationale, caveats, links)",
    )
    ui_hints: dict = field(
        required=False,
        description="Free-form object of UI hints for dashboard renderers",
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

        # name
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise SchemaValidationError(
                f"{cls.__name__}: 'name' is required and must be a "
                f"non-empty string"
            )
        if not _SNAKE_CASE_RE.match(name):
            raise SchemaValidationError(
                f"{cls.__name__}: 'name' must be lowercase snake_case, "
                f"got {name!r}"
            )

        # version
        version = payload.get("version")
        # Reject bool explicitly — bool is a subclass of int.
        if isinstance(version, bool) or not isinstance(version, int):
            raise SchemaValidationError(
                f"{cls.__name__}: 'version' is required and must be an integer"
            )
        if version < 1:
            raise SchemaValidationError(
                f"{cls.__name__}: 'version' must be >= 1, got {version}"
            )

        # summary
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary:
            raise SchemaValidationError(
                f"{cls.__name__}: 'summary' is required and must be a "
                f"non-empty string"
            )

        # ops
        ops = payload.get("ops")
        if not isinstance(ops, list) or not ops:
            raise SchemaValidationError(
                f"{cls.__name__}: 'ops' is required and must be a "
                f"non-empty list"
            )
        seen: set[str] = set()
        for i, op in enumerate(ops):
            op_name = _validate_op(op, i, cls.__name__)
            if op_name in seen:
                raise SchemaValidationError(
                    f"{cls.__name__}: duplicate op name {op_name!r}"
                )
            seen.add(op_name)

        # notes
        if "notes" in payload and not isinstance(payload["notes"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'notes' must be a string"
            )

        # ui_hints
        if "ui_hints" in payload and not isinstance(payload["ui_hints"], dict):
            raise SchemaValidationError(
                f"{cls.__name__}: 'ui_hints' must be an object"
            )
