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
contracts. For the v1 model (graph://86e04207-a25), review and merge-gate
concerns nest under ``source_control@1`` rather than appearing as
separate top-level contracts; the placeholder ``autonomy/github`` impl
declares ``source_control@1`` only. The full nested op inventory for
``source_control@1`` is finalized in a later bead.

All repo-local file/path fields (``package_root``, ``tool_paths``,
``skill_path``, ``primer_path``) are validated through
:func:`validate_repo_local_path` so later launch-time materialization can
trust the metadata for mounts and reads without re-validating.
"""

from __future__ import annotations

import posixpath
from typing import Any

from .registry import SchemaValidationError, SettingSchema, field, register_schema


SET_ID = "autonomy.capability.impl"
SCHEMA_REVISION = 1


VALID_DELIVERY_MODES = ("image_baked", "mounted_tools", "host_proxy", "hybrid")


SYNOPSIS = {
    "summary": (
        "Concrete capability implementation: provider, delivery mode, "
        "package root, probe, env/secret/tool requirements"
    ),
    "nouns": [
        "capability", "implementation", "impl", "provider",
        "delivery", "probe", "tool bundle", "package",
    ],
    "related_set_ids": [
        "autonomy.capability.contract#1",
        "autonomy.org.capability.install#1",
        "autonomy.workspace.capability.enable#1",
    ],
}


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
    "tool_target",
    "skill_path",
    "primer_path",
    "notes",
}

_TOOL_TARGET_REQUIRED = ("source", "target")
_TOOL_TARGET_ALLOWED = set(_TOOL_TARGET_REQUIRED) | {"expose_commands"}

_CONTRACT_REF_REQUIRED = ("contract", "version")
_CONTRACT_REF_ALLOWED = set(_CONTRACT_REF_REQUIRED)

_PROBE_REQUIRED = ("kind", "entrypoint")


def validate_repo_local_path(
    value: Any,
    *,
    field: str,
    cls_name: str,
) -> None:
    """Reject any path that is not a normalized, repo-local relative path.

    The intent is "the normalized path remains inside the repo-relative
    capability tree". Concretely we reject:

    * non-string or empty values
    * absolute paths (``/...`` or any drive-style ``C:\\...``)
    * any ``..`` parent-traversal segment (even if the normalized form
      would land back inside the tree — the segment itself signals
      escape intent and is unsafe to feed to a launch-time mount)
    * degenerate values (``.``, all-whitespace, paths whose normalized
      form is empty or ``.``)

    This helper is exported so other capability-related schemas can apply
    the same rule rather than duplicating ad hoc checks.
    """
    if not isinstance(value, str):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must be a string, got "
            f"{type(value).__name__}"
        )
    if not value or not value.strip():
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must be a non-empty repo-local path"
        )
    # Reject backslashes outright — capability paths are POSIX-style.
    if "\\" in value:
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must use forward slashes "
            f"(POSIX-style), got {value!r}"
        )
    if value.startswith("/"):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must be repo-local "
            f"(not absolute), got {value!r}"
        )
    # Drive-letter style absolutes (``C:\foo``) — defensive belt-and-braces.
    if len(value) >= 2 and value[1] == ":":
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must be repo-local "
            f"(not absolute), got {value!r}"
        )
    parts = value.split("/")
    if any(p == ".." for p in parts):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must not contain parent-traversal "
            f"('..') segments, got {value!r}"
        )
    normalized = posixpath.normpath(value)
    if normalized in (".", "", "/"):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must resolve to a non-empty "
            f"repo-local path, got {value!r}"
        )
    if normalized.startswith("../") or normalized == "..":
        # Defensive: even with the no-`..` rule above, fail closed if a
        # future loosening of that rule lets an escape through.
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must remain inside the repo-relative "
            f"capability tree, got {value!r}"
        )


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


def _validate_absolute_container_path(value: Any, *, field: str, cls_name: str) -> None:
    """Reject anything that is not a normalized absolute POSIX path.

    ``tool_target.target`` declares a stable runtime location inside the
    container (e.g. ``/opt/jira-tools``). It must be absolute so the
    launcher can mount the bundle deterministically without depending on
    the container's working directory.
    """
    if not isinstance(value, str):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must be a string, got {type(value).__name__}"
        )
    if not value or not value.strip():
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must be a non-empty absolute container path"
        )
    if "\\" in value:
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must use forward slashes (POSIX-style), "
            f"got {value!r}"
        )
    if not value.startswith("/"):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must be absolute (start with '/'), got {value!r}"
        )
    parts = value.split("/")
    if any(p == ".." for p in parts):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must not contain parent-traversal "
            f"('..') segments, got {value!r}"
        )
    normalized = posixpath.normpath(value)
    if normalized in ("/", ""):
        raise SchemaValidationError(
            f"{cls_name}: {field!r} must resolve to a non-root absolute "
            f"path, got {value!r}"
        )


def _validate_tool_target(payload: Any, cls_name: str) -> None:
    """Validate the optional ``tool_target`` shape.

    ``tool_target`` carries the runtime substrate that goes beyond
    ``tool_paths``: it declares an absolute container path where the
    bundle should be mounted, plus an optional list of ``expose_commands``
    that the launcher should make available on PATH via shim scripts.
    Together these fields express the Jira-style worked example
    (graph://86e04207-a25 § Example 2): tools at ``/opt/jira-tools`` plus
    PATH-visible ``jira-read`` / ``jira-comment`` / ``jira-create`` /
    ``jira-createmeta`` commands.
    """
    if not isinstance(payload, dict):
        raise SchemaValidationError(
            f"{cls_name}: 'tool_target' must be an object, got "
            f"{type(payload).__name__}"
        )
    extra = set(payload) - _TOOL_TARGET_ALLOWED
    if extra:
        raise SchemaValidationError(
            f"{cls_name}: 'tool_target' has unknown field(s): {sorted(extra)}"
        )
    for key in _TOOL_TARGET_REQUIRED:
        if key not in payload:
            raise SchemaValidationError(
                f"{cls_name}: 'tool_target' missing required field {key!r}"
            )
    validate_repo_local_path(
        payload["source"],
        field="tool_target.source",
        cls_name=cls_name,
    )
    _validate_absolute_container_path(
        payload["target"],
        field="tool_target.target",
        cls_name=cls_name,
    )
    expose = payload.get("expose_commands", [])
    if not isinstance(expose, list):
        raise SchemaValidationError(
            f"{cls_name}: 'tool_target.expose_commands' must be a list of "
            f"command names"
        )
    for i, cmd in enumerate(expose):
        if not isinstance(cmd, str) or not cmd:
            raise SchemaValidationError(
                f"{cls_name}: 'tool_target.expose_commands[{i}]' must be a "
                f"non-empty string"
            )
        if "/" in cmd or "\\" in cmd or cmd in (".", ".."):
            raise SchemaValidationError(
                f"{cls_name}: 'tool_target.expose_commands[{i}]' must be a "
                f"bare command name (no path separators), got {cmd!r}"
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
    ``tool_target``, ``skill_path``, ``primer_path``, ``notes``.

    ``tool_target`` (when present) is an object with required ``source``
    (repo-local path) and ``target`` (absolute container path) fields and
    an optional ``expose_commands`` list of bare command names. It lets a
    capability declare a stable non-package mount target — e.g. tools at
    ``/opt/jira-tools`` plus PATH-visible ``jira-read`` / ``jira-comment``
    / ``jira-create`` / ``jira-createmeta`` commands (graph://86e04207-a25
    § Example 2).

    Repo-local path fields (``package_root``, ``tool_paths``,
    ``skill_path``, ``primer_path``) are validated through
    :func:`validate_repo_local_path`.

    Migrated to the typed-``field()`` declaration shape (Bead 3B) —
    annotations now drive ``_field_metadata`` derivation via
    ``__init_subclass__``. Imperative validation in :meth:`validate`
    continues to enforce shape checks the typed metadata can't yet
    describe (path safety, probe sub-shape, ``tool_target`` sub-shape,
    contract-ref sub-shape, integer minimums, extra-field rejection).
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    name: str = field(
        required=True,
        description="Identifier for the implementation (e.g. autonomy/github)",
    )
    version: int = field(
        required=True,
        description="Single monotonic integer version (>= 1)",
    )
    implements: list = field(
        required=True,
        description="Contract refs this implementation satisfies",
        element={
            "contract": {"type": "string", "required": True,
                         "description": "Contract name"},
            "version": {"type": "integer", "required": True,
                        "description": "Pinned contract version"},
        },
    )
    delivery_mode: str = field(
        required=True,
        description="How the implementation reaches the workspace at launch",
        enum=list(VALID_DELIVERY_MODES),
    )
    package_root: str = field(
        required=True,
        description="Repo-local path that holds the implementation package",
    )
    probe: dict = field(
        required=True,
        description="Probe descriptor (kind + entrypoint) for runtime verification",
    )
    required_env: list = field(
        required=False,
        description="Env var names the implementation requires at runtime",
        element={"type": "string"},
    )
    required_secret_files: list = field(
        required=False,
        description="Secret-file paths the implementation requires at runtime",
        element={"type": "string"},
    )
    tool_paths: list = field(
        required=False,
        description="Repo-local paths to tools provided by this implementation",
        element={"type": "string"},
    )
    tool_target: dict = field(
        required=False,
        description=(
            "Optional tool-bundle mount target: source (repo-local), "
            "target (absolute container path), expose_commands (PATH shims)"
        ),
    )
    skill_path: str = field(
        required=False,
        description="Repo-local path to the implementation's skill bundle",
    )
    primer_path: str = field(
        required=False,
        description="Repo-local path to the implementation's primer doc",
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

        # package_root — required repo-local path
        if "package_root" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'package_root' is required"
            )
        validate_repo_local_path(
            payload["package_root"],
            field="package_root",
            cls_name=cls.__name__,
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

        # tool_paths — every entry must be a repo-local path.
        if "tool_paths" in payload:
            for i, entry in enumerate(payload["tool_paths"]):
                validate_repo_local_path(
                    entry,
                    field=f"tool_paths[{i}]",
                    cls_name=cls.__name__,
                )

        # tool_target — optional command-surface declaration.
        if "tool_target" in payload:
            _validate_tool_target(payload["tool_target"], cls.__name__)

        # Optional repo-local path fields.
        for key in ("skill_path", "primer_path"):
            if key in payload:
                validate_repo_local_path(
                    payload[key],
                    field=key,
                    cls_name=cls.__name__,
                )

        # Optional free-form string field.
        if "notes" in payload and not isinstance(payload["notes"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'notes' must be a string"
            )


register_schema(SET_ID, SCHEMA_REVISION, CapabilityImplV1)
