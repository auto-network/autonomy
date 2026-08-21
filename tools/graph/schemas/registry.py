"""Setting schema registry: contracts + upconverters.

A schema declares the shape of a Setting payload at one revision. An
upconverter is a pure function ``dict (rev N-1) -> dict (rev N)``.

The registry is keyed by the joined display form ``set_id#schema_revision``
(e.g. ``autonomy.workspace#1``). Storage keeps ``set_id`` and
``schema_revision`` split for index efficiency; lookups happen via
``schema_key(set_id, revision)``.

Breaking changes are expressed by *not* registering an upconverter for the
hop. Callers asking for ``target_revision >= N`` will silently drop
``stored_revision < N`` rows when no chain reaches them — see
``upconvert_chain``.

Schema-as-Setting (auto-82xyq): registered schemas are also upserted
into each per-org DB as ``autonomy.schema#1`` Settings, with their
hand-curated synopsis surfacing as ``autonomy.schema.synopsis#1``.
``flush_schema_meta(db)`` performs the upsert for one DB; the flush is
idempotent — payload match is a no-op.

Materialization is decoupled from ``_SCHEMA_USER_VERSION`` (auto-06ziz):
``flush_schema_meta_machine_store()`` flushes the machine store once,
and is invoked once at dashboard startup (the hot-reload restarts the
process on every code change, which is the only thing that can change the
registry). Adding a schema, editing a schema, or editing only a module's
SYNOPSIS is therefore live after a restart with no version bump and no
full table re-init. It is deliberately NOT called from
``GraphDB._init_schema`` — coupling it to the per-connection init path was
the bug: an already-initialized DB never re-flushed until someone bumped
the schema version, which also forced an expensive full re-init.

Suffix composition (auto-uqdkk): a subclass may declare
``set_id_suffix = "leaf"`` to inherit its parent's ``set_id`` as a
namespace prefix, composing ``parent.set_id + "." + suffix`` at class-
definition time and writing the result back into ``cls.__dict__``. The
namespace hierarchy lives in the inheritance graph rather than in
hand-typed dotted strings; substrate-level prefix matching covers
"all descendants of <namespace>". Three gotchas to keep in mind:

1. **Suffix-less concrete subclass silently inherits the parent's
   set_id.** A subclass that declares ``schema_revision`` but no
   ``set_id_suffix`` (and no own ``set_id``) does not auto-register —
   its ``set_id`` is inherited via attribute lookup, not present in its
   own ``__dict__``, so the inherited-attrs guard skips it. Either
   declare ``set_id_suffix`` (composed leaf) or an explicit ``set_id``
   (override the namespace).

2. **Diamond inheritance picks the leftmost branch's namespace.** With
   ``class C(A, B)`` where both ``A.set_id`` and ``B.set_id`` are set,
   the MRO walk takes ``A.set_id`` as the prefix — so
   ``C.set_id_suffix = "c"`` composes to ``"a.c"``, not ``"b.c"``.
   Mixin reorderings change which branch wins.

3. **Suffix renames ripple into descendants.** The composed ``set_id``
   is written back to ``cls.__dict__`` at class-definition time, so
   renaming a parent's ``set_id_suffix`` (or its ``set_id``) requires
   re-creating descendant classes for the new prefix to flow through.
   In practice that's just a process restart, but registry callers
   that cache classes across reloads must clear and re-register.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, get_origin
from uuid import uuid4


logger = logging.getLogger(__name__)


# ── Discriminator slug helper ────────────────────────────────
#
# Single shared CamelCase → snake_case helper used to derive variant
# discriminator slugs from subclass class names. ``__init_subclass__``
# uses this when registering a variant subclass under its parent's
# ``_variants`` registry; codegen consumers (1D + downstream) read the
# same slug from the meta-Setting payload. Both call sites point at
# this one helper so the conversion stays consistent.

_SLUG_TWO_UPPER = re.compile(r"(.)([A-Z][a-z]+)")
_SLUG_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(name: str) -> str:
    """Convert a CamelCase class name to a snake_case discriminator slug.

    Examples::

        ThumbYes        → thumb_yes
        RefreshRequest  → refresh_request
        Choice          → choice
        MyURL           → my_url
        URLToFoo        → url_to_foo

    Idempotent on already-snake-case input. Module-level so codegen and
    variant enumeration share the exact same conversion.
    """
    s1 = _SLUG_TWO_UPPER.sub(r"\1_\2", name)
    return _SLUG_LOWER_UPPER.sub(r"\1_\2", s1).lower()


class SchemaValidationError(ValueError):
    """Raised when a payload does not conform to its declared schema."""


# ── Typed field declarations (additive over _field_metadata) ──
#
# Subclasses can declare their schema fields as typed annotations with a
# :func:`field` value on the right-hand side, and ``__init_subclass__``
# derives ``_field_metadata`` from those declarations. The legacy
# ``_field_metadata`` dict still works unchanged; when both are present,
# the typed declarations take precedence per-key.
#
#     class WorkspaceV1(SettingSchema):
#         set_id = "autonomy.workspace"
#         schema_revision = 1
#
#         name:    str = field(required=True,
#                              description="Workspace identifier (matches key)")
#         image:   str = field(required=True,
#                              description="Container image to launch")
#         harness: str = field(default="claude",
#                              enum=["claude", "codex"],
#                              description="Agent CLI to launch")


_MISSING = object()


REMEDIATION_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\.v[1-9][0-9]*$"
)
_SECRET_BEARING_PARAM_NAMES = (
    "value", "password", "private_key", "secret_value",
)
JSONScalar = str | int | float | bool | None


@dataclass(frozen=True)
class RemediationRef:
    """A schema's data-only reference to trusted remediation behavior.

    The reference deliberately contains no callable and no input value. The
    code-owned remediation registry interprets the stable ID later; schema
    construction validates only this portable structural envelope.
    """

    id: str
    params: dict[str, JSONScalar] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = _normalize_remediation_ref_data({
            "id": self.id,
            "params": self.params,
        })
        object.__setattr__(self, "id", normalized["id"])
        object.__setattr__(self, "params", normalized["params"])


def _normalize_remediation_ref_data(value: Any) -> dict[str, Any]:
    """Return the canonical plain-data shape for one remediation reference.

    Error messages name only the invalid field/key and its type. They never
    interpolate a parameter value, because a structurally invalid declaration
    is still not permission to disclose whatever an author put there.
    """
    if isinstance(value, RemediationRef):
        value = {"id": value.id, "params": value.params}
    if not isinstance(value, dict):
        raise SchemaValidationError("remediation must be a RemediationRef or object")
    unknown = set(value) - {"id", "params"}
    if unknown:
        raise SchemaValidationError(
            f"remediation declares unknown field(s): {sorted(unknown)}"
        )
    remediation_id = value.get("id")
    if not isinstance(remediation_id, str) or not REMEDIATION_ID_PATTERN.fullmatch(
        remediation_id
    ):
        raise SchemaValidationError(
            "remediation id must match "
            "^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\\.v[1-9][0-9]*$ exactly"
        )
    params = value.get("params", {})
    if not isinstance(params, dict):
        raise SchemaValidationError("remediation params must be an object")
    normalized_params: dict[str, Any] = {}
    for key, param_value in params.items():
        if not isinstance(key, str) or not key:
            raise SchemaValidationError(
                "remediation parameter names must be non-empty strings"
            )
        lowered = key.lower()
        if any(forbidden in lowered for forbidden in _SECRET_BEARING_PARAM_NAMES):
            raise SchemaValidationError(
                f"remediation parameter {key!r} has a secret-bearing name"
            )
        if not (
            param_value is None
            or isinstance(param_value, (str, int, float, bool))
        ):
            raise SchemaValidationError(
                f"remediation parameter {key!r} must be a JSON scalar, got "
                f"{type(param_value).__name__}"
            )
        if isinstance(param_value, float) and not math.isfinite(param_value):
            raise SchemaValidationError(
                f"remediation parameter {key!r} must be a finite JSON number"
            )
        normalized_params[key] = param_value
    return {"id": remediation_id, "params": normalized_params}


def normalize_remediation_ref(value: Any) -> dict[str, Any]:
    """Public structural normalizer used by schemas and readiness hooks."""
    return _normalize_remediation_ref_data(value)


@dataclass(frozen=True)
class _FieldSpec:
    """Field metadata produced by :func:`field`. Plucked by
    :meth:`SettingSchema.__init_subclass__`.
    """
    required: bool | None = None
    default: Any = _MISSING
    default_factory: Callable[[], Any] | None = None
    description: str | None = None
    enum: list | None = None
    element: Any = None
    references: str | None = None
    reference_scope: str | None = None
    exists: str | None = None
    exists_frame: str | None = None
    names_host_env: bool = False
    env_fallback_field: str | None = None
    severity: str | None = None
    remediation: RemediationRef | dict[str, Any] | None = None


def field(
    *,
    required: bool | None = None,
    default: Any = _MISSING,
    default_factory: Callable[[], Any] | None = None,
    description: str | None = None,
    enum: list | None = None,
    element: Any = None,
    references: str | None = None,
    reference_scope: str | None = None,
    exists: str | None = None,
    exists_frame: str | None = None,
    names_host_env: bool = False,
    env_fallback_field: str | None = None,
    severity: str | None = None,
    remediation: RemediationRef | dict[str, Any] | None = None,
) -> Any:
    """Declare metadata for a SettingSchema field.

    Use as the right-hand side of a typed annotation:

        name: str = field(required=True, description="...")

    Parameters:
        required: explicit required flag. Defaults to True when neither
            ``default`` nor ``default_factory`` is given.
        default: literal default value (replaces the class attribute).
        default_factory: callable producing the default — invoked once
            at class-creation time and stored as the class attribute.
        description: human-readable description; surfaces in
            ``graph set schema`` and (eventually) generated IDE tooltips.
        enum: list of valid values for enum-shaped fields.
        element: per-element shape for list-typed fields. Bare Python
            types become JSON-schema type names; a dict of field→type
            describes a list-of-dict element shape; a ``SettingSchema``
            marked ``internal`` declares the element properly, and is the
            form that enforcement and reference checking can descend into.
        references: this field's value is a KEY in the named set. Declaring
            it is what lets a generic checker report that the thing being
            referred to has not been provisioned, without knowing anything
            about what either set means.
        reference_scope: ``"org"`` when the stored key is
            ``<org>:<value>`` rather than the value alone.
        exists: this value names something on a filesystem, and what kind:
            ``"file"``, ``"dir"`` or ``"executable"``. A READINESS check, and
            deliberately never run at write. Whether a file is present is a
            fact about the world rather than about the value: it differs
            between machines, changes after the write, and would make an
            organization's row refusable on one host and acceptable on
            another. Declaring it lets a check verb ask on demand.
        exists_frame: WHOSE filesystem the ``exists`` check is about, when
            it is not the process asking. ``"platform-host"`` says the path
            belongs to the machine running the platform -- a bind-mount
            source the docker daemon resolves, say -- so a container asking
            the question is looking at the wrong filesystem entirely. That
            matters more than it sounds: the wrong frame answers MISSING for
            a path that is present, and PRESENT for a path that is not,
            because a same-named directory inside the container satisfies
            it. The second is the dangerous one, since a check made green
            that way is silence, and silence reads as agreement.
        names_host_env: this value is the NAME of a host environment
            variable the workspace expects to be forwarded. Also a
            READINESS check. A launcher forwards only the variables that
            are actually set, and skips the rest in silence, so a
            workspace declaring one that is unset starts without it and
            fails much later at whatever needed it. Declaring this lets
            the check say which name is unset, in the environment it
            looked in -- which is the process running the check, and is
            not the launcher's unless they are the same process.
        env_fallback_field: sibling mapping whose keys are fixed environment
            values applied before ``names_host_env`` forwarding. A named
            variable already present there is satisfied even when the host
            process does not override it. This mirrors the launcher's
            effective-environment merge rather than checking one source in
            isolation. Valid only with ``names_host_env``.
        severity: what an UNSATISFIED value here means. Blocking by
            default: whoever declares a requirement is saying it is
            needed. ``"advisory"`` says the thing degrades gracefully
            instead of stopping -- a local clone source whose absence
            costs a network fetch, not a launch. Declared and never
            inferred, because a checker that guesses advisory reports a
            broken install as ready, and nobody re-reads a clean result.
        remediation: data-only reference to a trusted remediation contract.
            It may name a registered action family but cannot select code or
            contain an input value. Semantic registry validation happens in
            the planner/CI audit rather than at schema import time.

    ``description`` is required of every field a SHIPPED schema declares,
    asserted over the live registry rather than here — a throwaway schema
    in a test publishes no contract and needs none. See
    ``test_schemas_declare_contracts``.
    """
    return _FieldSpec(
        required=required,
        default=default,
        default_factory=default_factory,
        description=description,
        enum=enum,
        element=element,
        references=references,
        reference_scope=reference_scope,
        exists=exists,
        exists_frame=exists_frame,
        names_host_env=names_host_env,
        env_fallback_field=env_fallback_field,
        severity=severity,
        remediation=remediation,
    )


def _normalize_element(element: Any) -> Any:
    # A declared element shape is kept as the class here; the metadata pass
    # moves it to ``_element_schemas`` and leaves plain data behind.
    if isinstance(element, type) and issubclass(element, SettingSchema):
        return element
    if isinstance(element, type):
        return _python_type_to_json_type(element)
    if isinstance(element, dict):
        return {
            k: _python_type_to_json_type(v) if isinstance(v, type) else v
            for k, v in element.items()
        }
    return element


def _build_metadata_from_spec(ann: Any, spec: _FieldSpec) -> dict:
    meta: dict[str, Any] = {"type": _python_type_to_json_type(ann)}
    if spec.description is not None:
        meta["description"] = spec.description
    if spec.required is True:
        meta["required"] = True
    elif spec.required is None and spec.default is _MISSING \
            and spec.default_factory is None:
        meta["required"] = True
    if spec.default is not _MISSING:
        meta["default"] = spec.default
    if spec.enum is not None:
        meta["enum"] = list(spec.enum)
    if spec.element is not None:
        meta["element"] = _normalize_element(spec.element)
    if spec.references is not None:
        meta["references"] = spec.references
    if spec.reference_scope is not None:
        meta["reference_scope"] = spec.reference_scope
    if spec.exists is not None:
        if spec.exists not in VALID_EXISTS:
            raise SchemaValidationError(
                f"exists must be one of {list(VALID_EXISTS)}, got {spec.exists!r}"
            )
        meta["exists"] = spec.exists
    if spec.exists_frame is not None:
        if spec.exists_frame not in VALID_EXISTS_FRAMES:
            raise SchemaValidationError(
                f"exists_frame must be one of {list(VALID_EXISTS_FRAMES)}, "
                f"got {spec.exists_frame!r}"
            )
        if spec.exists is None:
            raise SchemaValidationError(
                "exists_frame says whose filesystem an exists check reads, "
                "and this field declares no exists check"
            )
        meta["exists_frame"] = spec.exists_frame
    if spec.names_host_env:
        meta["names_host_env"] = True
    if spec.env_fallback_field is not None:
        if not spec.names_host_env:
            raise SchemaValidationError(
                "env_fallback_field names the fixed environment used by a "
                "host-env readiness check, but this field does not declare "
                "names_host_env"
            )
        if not isinstance(spec.env_fallback_field, str) or not spec.env_fallback_field:
            raise SchemaValidationError(
                "env_fallback_field must be a non-empty sibling field name"
            )
        meta["env_fallback_field"] = spec.env_fallback_field
    if spec.severity is not None:
        if spec.severity not in VALID_SEVERITIES:
            raise SchemaValidationError(
                f"severity must be one of {list(VALID_SEVERITIES)}, "
                f"got {spec.severity!r}"
            )
        meta["severity"] = spec.severity
    if spec.remediation is not None:
        meta["remediation"] = normalize_remediation_ref(spec.remediation)
    return meta


# ── Access-pattern decorators ─────────────────────────────────
#
# A schema's access semantics are a property of the schema, not of every
# page that uses it. Decorators store the pattern + key strategy on the
# class so codegen consumers (CLI introspection, JS proxy, generated
# typed methods) can dispatch on them.
#
# Three patterns cover the substrate's current consumers:
#
#   @append_only_log          uuid keys, never overridden, .append(payload)
#   @singleton(key="default") fixed key, latest-write-wins, .set(payload)
#   @keyed_per_entity         caller-supplied natural key, .upsert(key, payload)
#
# A schema without a decorator is a generic keyed Setting — the
# substrate's escape hatch (.write({key, payload})). The decorators set
# class-level ``_access_pattern`` (str) and ``_key_strategy`` (str)
# attributes; consumers read those. ``export_json_schema``'s payload
# expansion to surface this metadata is bead 1D; 1B only stores it.


def _claim_access_pattern(target: type, pattern: str, key_strategy: str) -> type:
    """Stamp the cardinality declaration, refusing a second one.

    The three access-pattern decorators are alternatives: a schema has one
    cardinality. Stacking two silently resolved to whichever sat outermost and
    left the class claiming a cardinality nobody chose, so it is refused here
    instead. Checks the class's OWN ``__dict__`` — a variant subclass
    legitimately inherits its parent's pattern and may declare its own.
    """
    existing = target.__dict__.get("_access_pattern")
    if existing is not None and existing != pattern:
        raise SchemaValidationError(
            f"{target.__name__}: declares two access patterns "
            f"({existing!r} and {pattern!r}). A schema has one cardinality; "
            f"pick the decorator that matches what its writers actually write."
        )
    target._access_pattern = pattern
    target._key_strategy = key_strategy
    return target


def append_only_log(
    cls: type | None = None,
    *,
    key: str | Callable[..., Any] = "uuid_v4",
) -> Any:
    """Schema decorator: declare append-only event log semantics.

    Rows are never overridden; each write generates a fresh key via the
    named key strategy (default ``uuid_v4``). Codegen-aware consumers
    expose ``.append(payload)`` instead of generic ``.write({key,
    payload})``.

    The design signpost shows ``@append_only_log(key=uuid_v4)`` (a
    callable reference) as the canonical authoring shape. We normalize
    callables to their ``__name__`` at decoration time so
    ``_key_strategy`` always holds a string for downstream serialization
    and pattern-matching.

    Usable as ``@append_only_log`` (bare) or
    ``@append_only_log(key="...")``.
    """
    if callable(key):
        key_name = getattr(key, "__name__", repr(key))
    else:
        key_name = str(key)

    def _wrap(target: type) -> type:
        return _claim_access_pattern(target, "append_only_log", key_name)

    if cls is None:
        return _wrap
    return _wrap(cls)


def singleton(cls: type | None = None, *, key: str = "default") -> Any:
    """Schema decorator: declare latest-write-wins semantics on a fixed key.

    Codegen-aware consumers expose ``.set(payload)`` instead of generic
    ``.write({key, payload})``. The key defaults to ``"default"`` so the
    common case is parameter-free.

    Usable as ``@singleton`` (bare) or ``@singleton(key="...")``.
    """
    def _wrap(target: type) -> type:
        return _claim_access_pattern(target, "singleton", f"fixed:{key}")

    if cls is None:
        return _wrap
    return _wrap(cls)


def keyed_per_entity(
    cls: type | None = None,
    *,
    key_strategy: str = "natural",
    key_references: dict[str, str] | None = None,
) -> Any:
    """Schema decorator: declare per-entity rows with caller-supplied keys.

    Codegen-aware consumers expose ``.upsert(key, payload)`` instead of
    generic ``.write({key, payload})``. ``key_strategy`` names the
    caller-side convention (``natural`` = caller picks; future
    strategies can name structured-key derivations).

    ``key_references`` says which SET each named key segment identifies —
    ``{"workspace_id": "autonomy.workspace"}`` declares that the first
    segment of every key is a row in ``autonomy.workspace``. The strategy
    names the segments; this states what those names identify.

    Declared, the edge is traversable from the entity: given a workspace,
    every row keyed by it can be found. That is what answers "is this
    workspace fully installed" from one address, and what makes a row whose
    key names a deleted entity reportable.

    Usable as ``@keyed_per_entity`` (bare) or
    ``@keyed_per_entity(key_strategy="...", key_references={...})``.
    """
    def _wrap(target: type) -> type:
        out = _claim_access_pattern(target, "keyed_per_entity", key_strategy)
        if key_references:
            segments = [seg.strip("[]") for seg in re.split(r"[:/]", key_strategy)]
            unknown = sorted(set(key_references) - set(segments))
            if unknown:
                raise SchemaValidationError(
                    f"{target.__name__}: key_references names segment(s) "
                    f"{unknown} that the key strategy {key_strategy!r} does "
                    f"not have — it declares {segments}"
                )
            out._key_references = dict(key_references)
        return out

    if cls is None:
        return _wrap
    return _wrap(cls)


VALID_HOMES = ("machine", "personal", "organization")

#: What a declared ``exists`` check asserts about a filesystem entry. Checked
#: on demand by a readiness verb, never at write -- see ``field(exists=...)``.
VALID_EXISTS = ("file", "dir", "executable")

#: Whose filesystem an ``exists`` check is about. Absent means the process
#: asking. ``platform-host`` means the machine running the platform, which a
#: container is not -- and cannot answer for.
VALID_EXISTS_FRAMES = ("platform-host",)

#: What an unsatisfied requirement means. ``blocking`` is the default and is
#: never declared; ``advisory`` has to be, because guessing it is the error
#: that reports a broken install as ready.
VALID_SEVERITIES = ("blocking", "advisory")

#: Vault tiers a schema may declare -- WHO MUST PARTICIPATE to read the value
#: back (``0c206bd8-1c6`` §4.1). ``audited`` releases to any authorized
#: session; ``secured`` additionally requires the human factor its policy
#: class carries. Both store the payload as an encrypted storage object rather
#: than as plain JSON in the row.
VALID_VAULT_TIERS = ("audited", "secured")


#: Publication states in increasing order of reach. A row's state decides who
#: may read it across an organization boundary; nothing else does.
PUBLICATION_ORDER = ("raw", "curated", "published", "canonical")


def publication_band(*, min: str = "raw", max: str = "canonical") -> Any:
    """Schema decorator: the publication states this set's rows may hold.

    Publication state is the only control over cross-organization reads, and
    until now nothing constrained it per set. The same axis was wrong in both
    directions at once: every capability contract sat at ``raw``, so another
    organization's install of it could not resolve, while nothing stopped a
    sealed credential being promoted to ``published``, where every peer reads
    it. One of those is an outage and the other is a disclosure, and neither
    announces itself.

    A band says what the set is FOR. ``max="raw"`` means these rows never
    leave the database that owns them, whatever anyone later types. A shared
    definition can require the opposite with ``min="published"``, so a row
    nobody can build against is refused at the moment it is written rather
    than discovered by whoever could not read it.

    Enforced on every write and on promotion, because a band checked only at
    creation is a band a promotion walks through.

    Undeclared means unconstrained -- a schema that has not been through this
    decision behaves exactly as it did.
    """
    lo, hi = min, max
    for name, value in (("min", lo), ("max", hi)):
        if value not in PUBLICATION_ORDER:
            raise SchemaValidationError(
                f"publication_band {name} must be one of "
                f"{list(PUBLICATION_ORDER)}, got {value!r}"
            )
    if PUBLICATION_ORDER.index(lo) > PUBLICATION_ORDER.index(hi):
        raise SchemaValidationError(
            f"publication_band min {lo!r} is above max {hi!r}: no state "
            f"satisfies it, so every write would be refused"
        )

    def _wrap(target: type) -> type:
        target._publication_band = (lo, hi)
        return target

    return _wrap


def declared_band(set_id: str, revision: int) -> tuple[str, str] | None:
    """The band declared for ``set_id#revision``, or None if unconstrained."""
    schema = get_schema(set_id, int(revision))
    return getattr(schema, "_publication_band", None) if schema else None


def states_allowed(set_id: str, revision: int) -> tuple[str, ...]:
    """Every publication state this set may hold, widest-first for messages."""
    band = declared_band(set_id, revision)
    if band is None:
        return PUBLICATION_ORDER
    lo, hi = band
    return PUBLICATION_ORDER[
        PUBLICATION_ORDER.index(lo):PUBLICATION_ORDER.index(hi) + 1]


def home(where: str) -> Any:
    """Schema decorator: declare which database this Setting lives in.

    ``machine`` is this computer and nothing else: a path a binary landed at
    here, what is installed here, what was verified here. ``personal`` is the
    operator's own store, which follows them across every machine they own.
    ``organization`` is a store an org owns and that its members read.

    The distinction is not cosmetic. An organization's database is what
    federates, so a value put in the wrong one is either invisible to
    everyone who needs it or visible to everyone who should not have it. And
    a machine fact placed in the personal store becomes wrong on every
    machine but the one that wrote it -- a recorded path to a binary that
    another host does not have, believed rather than probed.

    Stacked ABOVE the access-pattern decorator, and separate from it because
    the two answer different questions: how many rows there are, and whose
    database they are in. Neither implies the other.

    Undeclared means undeclared -- nothing is asserted, and a schema that
    has not been through this decision behaves exactly as it did.
    """
    if where not in VALID_HOMES:
        raise SchemaValidationError(
            f"home must be one of {list(VALID_HOMES)}, got {where!r}"
        )

    def _wrap(target: type) -> type:
        existing = target.__dict__.get("_home")
        if existing is not None and existing != where:
            raise SchemaValidationError(
                f"{target.__name__}: declares two homes "
                f"({existing!r} and {where!r}); it lives in one database"
            )
        target._home = where
        return target

    return _wrap


def readiness_gated_by(field_name: str) -> Any:
    """Schema decorator: a payload field says whether this row is required.

    Some rows describe something optional at the level of the whole row
    rather than field by field — a mount the container starts happily
    without, say. That fact is already in the payload, written by whoever
    declared the row; what is missing is any way for a generic check to know
    which field carries it.

    Declaring the field name is that way. When the named field is falsy, every
    finding from the row is advisory rather than blocking. The checker still
    reports it — an optional thing being absent is worth saying — it just
    stops claiming the launch is broken.

    Named rather than inferred. "required" is a plausible convention and a
    schema is free to call it something else, and a checker that guessed
    would silently downgrade real findings on any row that happened to have
    a falsy field by that name.
    """

    def _wrap(target: type) -> type:
        existing = target.__dict__.get("_readiness_gate")
        if existing is not None and existing != field_name:
            raise SchemaValidationError(
                f"{target.__name__}: declares two readiness gates "
                f"({existing!r} and {field_name!r})"
            )
        meta = target.__dict__.get("_field_metadata") or {}
        if meta and field_name not in meta:
            raise SchemaValidationError(
                f"{target.__name__}: readiness gate names {field_name!r}, "
                f"which is not a field it declares"
            )
        target._readiness_gate = field_name
        return target

    return _wrap


def readiness_gate(set_id: str, revision: int) -> str | None:
    """The payload field naming whether a row of this set is required."""
    schema = get_schema(set_id, revision)
    return getattr(schema, "_readiness_gate", None) if schema else None


def declared_home(set_id: str) -> str | None:
    """The home every registered revision of ``set_id`` agrees on.

    A Setting does not move between databases when its schema gains a
    revision, so disagreement is a contradiction rather than something to
    resolve by picking the newest.
    """
    prefix = f"{set_id}#"
    seen = {
        cls._home
        for key, cls in SCHEMAS.items()
        if key.startswith(prefix) and getattr(cls, "_home", None) is not None
    }
    if not seen:
        return None
    if len(seen) > 1:
        raise SchemaValidationError(
            f"{set_id}: revisions declare different homes {sorted(seen)}; "
            f"a Setting does not change database between revisions"
        )
    return seen.pop()


def vaulted(tier: str) -> Any:
    """Schema decorator: this set's payloads are secrets, stored encrypted.

    A row of a vaulted set does not hold its payload. The payload is
    encrypted once as a content object under the organization's current key
    generation and the row keeps a locator (``tools.vault.storage_object``),
    so whatever can read the database file learns which object a setting is
    and nothing about what it says.

    ``tier`` names WHO MUST PARTICIPATE to read it back — ``audited`` releases
    to any authorized session, ``secured`` additionally requires the human
    factor. It is a property of the KIND of value, which is why it is declared
    here once rather than passed at every write, where one forgetful call site
    would silently write a credential in the clear.

    Declaring it is not sufficient to write one: the writer needs the domain's
    key control, which ``settings_ops.set_vault_sealer`` injects. A vaulted
    write with no sealer registered is REFUSED, never downgraded to plaintext.
    """
    if tier not in VALID_VAULT_TIERS:
        raise SchemaValidationError(
            f"vault tier must be one of {list(VALID_VAULT_TIERS)}, got {tier!r}"
        )

    def _wrap(target: type) -> type:
        existing = target.__dict__.get("_vault_tier")
        if existing is not None and existing != tier:
            raise SchemaValidationError(
                f"{target.__name__}: declares two vault tiers "
                f"({existing!r} and {tier!r}); a value has one release rule"
            )
        target._vault_tier = tier
        return target

    return _wrap


def declared_vault_tier(set_id: str) -> str | None:
    """The vault tier every registered revision of ``set_id`` agrees on.

    Disagreement is refused rather than resolved by picking the newest: a
    revision bump that quietly weakened the release rule of already-written
    secrets would be invisible at the call site that relies on it.
    """
    prefix = f"{set_id}#"
    seen = {
        cls._vault_tier
        for key, cls in SCHEMAS.items()
        if key.startswith(prefix) and getattr(cls, "_vault_tier", None) is not None
    }
    if not seen:
        return None
    if len(seen) > 1:
        raise SchemaValidationError(
            f"{set_id}: revisions declare different vault tiers {sorted(seen)}; "
            f"a secret does not change its release rule between revisions"
        )
    return seen.pop()


VALID_SIGNER_TIERS = ("persona", "delegate")


def signer(tier: str) -> Any:
    """Schema decorator: which key signs this set's ORGANIZATION rows.

    Every row in an organization database is signed — that follows the
    STORE, not the set, and this declaration cannot opt out of it (nor does
    it constrain where the set lives: an org-homed set may still hold the
    operator's own unsigned row in ``personal.db``). What a schema declares
    here is WHICH KEY signs where signing applies:

    - ``persona`` — the acting persona, derived from the personal root and
      therefore requiring an unlock. A value of this set may not be chosen
      with nobody present (D8).
    - ``delegate`` — the attenuated agent delegate, unattended (D7).

    ``delegate`` is the common case and the default; the declaration exists
    so choosing PERSONA is deliberate and choosing delegate is at least
    visible. Distinct from ``key_strategy``, which answers where
    verification resolves the signer FROM, not which key signs.
    """
    if tier not in VALID_SIGNER_TIERS:
        raise SchemaValidationError(
            f"signer tier must be one of {list(VALID_SIGNER_TIERS)}, got {tier!r}"
        )

    def _wrap(target: type) -> type:
        existing = target.__dict__.get("_signer_tier")
        if existing is not None and existing != tier:
            raise SchemaValidationError(
                f"{target.__name__}: declares two signer tiers "
                f"({existing!r} and {tier!r}); a revision has one signer"
            )
        target._signer_tier = tier
        return target

    return _wrap


def declared_signer(set_id: str, revision: int) -> str:
    """The signing tier governing rows of ``set_id`` AT ``revision``.

    REVISION-AWARE, unlike :func:`declared_vault_tier`, and deliberately so:
    a row stores its revision and is governed by that revision's
    declaration, so two revisions of one set may declare different tiers
    and both resolve — attendance is a property of how a value is CHOSEN,
    decided per contract generation, where a vault tier is a property of
    secrets already at rest, which a revision bump must not quietly weaken.

    Silence resolves to ``delegate`` — the common case, and not an error;
    the decorator exists so ``persona`` is a deliberate act. An
    unregistered revision also resolves to ``delegate``: what a boundary
    does with a set that has no schema at all is the boundary's policy,
    not this resolver's.
    """
    return signer_declaration(set_id, revision)["tier"]


def signer_declaration(set_id: str, revision: int) -> dict:
    """``{tier, explicit}`` — the signing tier AND whether it was declared.

    :func:`declared_signer` deliberately collapses silence into
    ``delegate``; this is the introspection that keeps the collapse
    AUDITABLE. A sensitive schema that forgot ``@signer("persona")`` signs
    unattended and looks identical to a reviewed ``@signer("delegate")``
    in the resolved tier — here the two differ: ``explicit`` is whether
    any revision-class on the MRO declared a tier, so "defaulted" is a
    listable fact rather than an invisible one. An unregistered revision
    is ``{tier: "delegate", explicit: False}``.
    """
    cls = SCHEMAS.get(f"{set_id}#{revision}")
    tier = getattr(cls, "_signer_tier", None) if cls is not None else None
    return {
        "tier": tier if tier is not None else "delegate",
        "explicit": tier is not None,
    }


def cache(
    cls: type | None = None,
    *,
    ttl: timedelta,
) -> Any:
    """Schema decorator: declare cache semantics with TTL-driven GC.

    A cache schema's rows carry an absolute ``expires_at`` set at
    write time (``updated_at + ttl``). The cache_gc cron sweeps rows
    whose expires_at has elapsed and whose publication_state is
    below ``published``.

    ``ttl`` is required — there is no default-eternal cache.
    ``timedelta`` is enforced (not int seconds) so the unit is
    explicit at the call site. Cardinality is declared separately by
    stacking an access-pattern decorator; this one says nothing about it.

    Usable only with parens: ``@cache(ttl=timedelta(...))``.
    """
    if not isinstance(ttl, timedelta):
        raise TypeError("@cache requires ttl=timedelta(...)")
    seconds = int(ttl.total_seconds())
    if seconds <= 0:
        raise ValueError("@cache requires a positive ttl")

    def _wrap(target: type) -> type:
        # Writes ONLY its own attribute. Caching policy and cardinality are
        # orthogonal -- a cached value can be a singleton, keyed per entity or
        # an append-only log -- so @cache stacks above whichever access-pattern
        # decorator applies rather than occupying that slot. It previously set
        # _access_pattern, which made the two mutually exclusive and silently
        # destroyed a real key strategy when stacked.
        target._cache_ttl_seconds = seconds
        return target

    if cls is None:
        return _wrap
    return _wrap(cls)


def _register_variant(cls: type) -> None:
    """Register *cls* under its parent's ``_variants`` map if applicable.

    A class whose immediate parent is ``SettingSchema`` is a fresh
    schema base, not a variant. A class whose immediate parent is some
    OTHER ``SettingSchema`` descendant is a variant of that parent: we
    derive the discriminator slug via :func:`snake_case` and store
    both the registry entry on the parent and ``_variant_slug`` on the
    class.

    Multi-level hierarchies preserve the tree shape — each level
    registers only its direct children. Nested-namespace consumers
    walk the tree to assemble dotted paths; the registry is not
    flattened.

    Looks up ``SettingSchema`` in module globals at call time to side-
    step the forward-reference issue (this function is defined before
    ``SettingSchema``, but it runs only via ``__init_subclass__`` —
    after the base class is fully constructed).
    """
    base = globals().get("SettingSchema")
    if base is None:  # SettingSchema not yet defined (shouldn't happen)
        return
    parent = cls.__mro__[1]
    if parent is base or not isinstance(parent, type):
        return
    if not issubclass(parent, base):
        return
    slug = snake_case(cls.__name__)
    parent._variants[slug] = cls
    cls._variant_slug = slug


def _extract_element_schemas(cls: type) -> None:
    """Move declared element shapes out of the JSON metadata.

    ``_field_metadata`` is serialized -- it is written into every org
    database by the schema-meta flush and exported as JSON schema -- so a
    class cannot live in it. The class goes to ``_element_schemas``, where
    enforcement and reference checking read it, and the metadata keeps the
    element's field shape as plain data, exactly as a hand-written dict
    would have.
    """
    meta = cls.__dict__.get("_field_metadata")
    if not meta:
        return
    element_schemas = dict(getattr(cls, "_element_schemas", None) or {})
    for name, spec in meta.items():
        element = spec.get("element")
        if isinstance(element, type) and issubclass(element, SettingSchema):
            element_schemas[name] = element
            spec["element"] = {
                sub: {k: v for k, v in sub_spec.items() if k != "element"}
                for sub, sub_spec in (
                    getattr(element, "_field_metadata", None) or {}
                ).items()
            }
    if element_schemas:
        cls._element_schemas = element_schemas


def _normalize_declared_remediations(cls: type) -> None:
    """Normalize legacy dict declarations as typed ``field()`` already does."""
    meta = cls.__dict__.get("_field_metadata")
    if not meta:
        return
    normalized: dict[str, dict] = {}
    for name, raw_spec in meta.items():
        spec = dict(raw_spec)
        if spec.get("remediation") is not None:
            spec["remediation"] = normalize_remediation_ref(spec["remediation"])
        normalized[name] = spec
    cls._field_metadata = normalized


def _compose_set_id_from_suffix(cls: type) -> None:
    """Compose ``cls.set_id`` from ``cls.set_id_suffix`` plus an ancestor's
    namespace ``set_id``.

    A subclass declaring its own ``set_id_suffix`` (and not its own
    ``set_id``) inherits the namespace from the nearest MRO ancestor whose
    ``__dict__`` carries a non-empty ``set_id``. The composed value is
    ``"<prefix>.<suffix>"`` and is written into ``cls.__dict__`` so
    descendants find it via their own MRO walk — composition is path-
    dependent, with each level appending one suffix to the running prefix.

    Skips silently when *cls* doesn't declare its own ``set_id_suffix``,
    or when *cls* declares its own ``set_id`` (explicit override wins).

    Raises ``TypeError`` when ``set_id_suffix`` is declared but no ancestor
    provides a namespace ``set_id``.
    """
    suffix = cls.__dict__.get("set_id_suffix")
    if not suffix:
        return
    if cls.__dict__.get("set_id"):
        return
    prefix: str | None = None
    for ancestor in cls.__mro__[1:]:
        own = ancestor.__dict__.get("set_id")
        if own:
            prefix = own
            break
    if prefix is None:
        raise TypeError(
            f"{cls.__qualname__}: set_id_suffix={suffix!r} declared but no "
            "ancestor provides a namespace set_id; declare set_id on a "
            "parent before composing suffixes from descendants."
        )
    cls.set_id = f"{prefix}.{suffix}"


# ── Mediator-action marker decorators ────────────────────────
#
# Substrate consumers wire mediator actions today via
# ``@register_action_decorator(SET_ID, predicate=..., name=...)`` applied
# to a free function in a separate ``_actions.py`` module. The marker
# decorators below let the action live INSIDE the schema class so the
# schema-action relationship is visible from the schema's own definition,
# without repeating the ``set_id`` string at every handler.
#
#     @keyed_per_entity
#     class WidgetV1(SettingSchema):
#         set_id = "dashboard.widget"
#         schema_revision = 1
#
#         @action
#         async def deliver(row, svc): ...
#
#         @on_kind("ping")
#         async def ping(row, svc): ...
#
# ``__init_subclass__`` walks the class body, finds methods bearing the
# ``_is_substrate_action`` / ``_action_kind`` markers, and registers
# each via ``register_action_decorator`` under the schema's ``set_id``.
# Default-action methods register with ``predicate=None``;
# ``@on_kind`` methods register with a closure predicate
# ``lambda r: r.get("kind") == kind``. Existing free-function consumers
# of ``@register_action_decorator`` continue to work unchanged — the
# markers are an additional path, not a replacement.


def action(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Marker decorator: the schema's default mediator-action handler.

    Stamps ``_is_substrate_action = True`` on *fn*. Discovery in
    :meth:`SettingSchema.__init_subclass__` registers each marked method
    via ``register_action_decorator(cls.set_id, name=f"{cls.__name__}.{method}")``
    with no predicate — the handler fires on every ``setting.changed``
    event for the schema's ``set_id``.
    """
    fn._is_substrate_action = True
    return fn


def on_kind(kind: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Marker decorator factory: the action for one ``kind`` variant.

    Always parameterized: ``@on_kind("thumb_yes")``. Stamps
    ``_action_kind`` on the decorated function; discovery wraps the
    handler in a ``row.get("kind") == kind`` predicate so multiple kinds
    can coexist on the same ``set_id`` without the substrate seeing them
    fire on every event.
    """
    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn._action_kind = kind
        return fn
    return _wrap


def _discover_actions(cls: type) -> None:
    """Find ``@action`` / ``@on_kind`` marked methods in ``cls.__dict__``
    and register each via the settings-mediator substrate.

    Only ``cls.__dict__`` is scanned — inherited methods are not
    re-registered when a subclass is defined. Non-callable values
    bearing a marker (defensive: someone tags a dataclass field by
    hand) are silently skipped.

    The substrate import is lazy so ``tools.graph.schemas`` doesn't pull
    ``tools.dashboard.settings_mediator`` until a schema actually has
    marked methods to register. Schemas that ship in
    ``tools/graph/schemas/*`` carry no markers today, so the lazy import
    only fires when plugin schemas (already inside
    ``tools.dashboard.*``) are imported.
    """
    set_id = cls.__dict__.get("set_id") or getattr(cls, "set_id", None)
    if not set_id:
        return
    marks: list[tuple[str, Callable[..., Any], str | None]] = []
    for attr_name, value in list(cls.__dict__.items()):
        if not callable(value):
            continue
        if getattr(value, "_is_substrate_action", False):
            marks.append((attr_name, value, None))
        elif hasattr(value, "_action_kind"):
            marks.append((attr_name, value, getattr(value, "_action_kind")))
    if not marks:
        return
    from tools.dashboard.settings_mediator import register_action_decorator
    for method_name, fn, kind in marks:
        registered_name = f"{cls.__name__}.{method_name}"
        if kind is None:
            register_action_decorator(set_id, name=registered_name)(fn)
        else:
            register_action_decorator(
                set_id,
                predicate=(lambda r, k=kind: r.get("kind") == k),
                name=registered_name,
            )(fn)


def _auto_register_schema(cls: type) -> None:
    """Auto-register *cls* in the schema registry when it declares both
    ``set_id`` and ``schema_revision`` in its own ``__dict__``.

    Skips silently when either is missing — that's an abstract intermediate
    base, not a concrete schema. ``register_schema`` is idempotent on the
    same ``(set_id, revision, class)`` triple, so any explicit call left
    in a module ends up re-registering the same row.

    A classmethod named ``upconvert_from_prev`` declared on the subclass
    is reflected and forwarded to ``register_schema``'s optional param.
    The classmethod must be on this class's own ``__dict__`` (inherited
    classmethods are skipped) so a subclass doesn't accidentally inherit
    its parent's upconverter.
    """
    if getattr(cls, "internal", False):
        return
    set_id = cls.__dict__.get("set_id")
    schema_revision = cls.__dict__.get("schema_revision")
    if not set_id or not schema_revision:
        return
    upconvert_fn: Callable[[dict], dict] | None = None
    if "upconvert_from_prev" in cls.__dict__:
        # classmethod descriptors expose the bound function via getattr.
        upconvert_fn = getattr(cls, "upconvert_from_prev", None)
    register_schema(
        set_id, int(schema_revision), cls,
        upconvert_from_prev=upconvert_fn,
    )


def _merged_inherited_field_metadata(cls: type) -> dict[str, dict]:
    """Return inherited ``_field_metadata`` merged across the MRO.

    Subclasses should see every field their parents declared, regardless of
    whether the parent used the legacy dict form or typed ``field()``
    declarations. Walk the MRO from oldest ancestor to nearest parent so the
    most-specific inherited class wins before the current subclass applies its
    own overrides.
    """
    merged: dict[str, dict] = {}
    for base_cls in reversed(cls.__mro__[1:]):
        meta = getattr(base_cls, "_field_metadata", None)
        if meta:
            merged.update(dict(meta))
    return merged


# ── JSON-schema field shape helpers ──────────────────────────


_JSON_TYPE_TO_PY: dict[str, Any] = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "array": list,
    "object": dict,
}


_PY_TO_JSON_TYPE = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    list: "array",
    dict: "object",
}


def _python_type_to_json_type(t: Any) -> str:
    """Map a Python type (or tuple of types) to a JSON-schema type name.

    A parameterized generic — ``list[str]`` rather than ``list`` — is not
    the bare type this mapping is keyed on, so resolve its origin before
    giving up. Without that, an annotation that resolved perfectly well
    falls through to ``"string"`` and types a list as a scalar, which
    every consumer of the metadata then believes.

    The final fallback stays deliberately: when ``get_type_hints`` cannot
    resolve a class's annotations it logs and leaves them as raw strings,
    and those degrade uniformly to ``"string"`` rather than being parsed
    back into half-trusted types.
    """
    if isinstance(t, tuple):
        for cand in t:
            mapped = _PY_TO_JSON_TYPE.get(cand)
            if mapped is not None:
                return mapped
        return "string"
    if t is Any:
        # A field genuinely of any type carries no type constraint. Recording
        # it as "string" is simply false, and enforcement then rejects the
        # very values it exists to allow.
        return "any"
    mapped = _PY_TO_JSON_TYPE.get(t)
    if mapped is not None:
        return mapped
    origin = get_origin(t)
    if origin is not None:
        mapped = _PY_TO_JSON_TYPE.get(origin)
        if mapped is not None:
            return mapped
    return "string"


class SettingSchema:
    """Base class for Setting payload schemas.

    Subclasses set class attributes ``set_id`` and ``schema_revision`` and
    override ``validate`` to enforce shape. The default ``validate`` is a
    no-op so registry round-trips work in tests without writing a real
    contract.

    For schema introspection (``graph set schema/example/find``), subclasses
    declare ``_field_metadata`` — a per-field dict carrying description,
    type, required flag, enum choices, default value, and (for arrays)
    element shape. ``export_json_schema()`` synthesizes a json-schema
    dict from this metadata.

    A schema whose integrity spans adjacent version fields or conventional
    key joins may additionally declare ``readiness_findings(*, key, payload,
    org, read)``.  The generic checker supplies its resolved-row reader and
    converts the returned data-only issues into normal findings.  The hook is
    evaluated on demand, never during a write, for the same reason as
    ``field(exists=...)``.
    """

    set_id: str = ""
    schema_revision: int = 0

    # Optional namespace leaf — when set on a subclass (and ``set_id`` is
    # not), :func:`_compose_set_id_from_suffix` composes the subclass's
    # ``set_id`` as ``parent_namespace + "." + set_id_suffix`` at class-
    # definition time. See module docstring for the three gotchas.
    set_id_suffix: str = ""

    # Field metadata: ``{field_name: {description, type, required,
    # enum, default, element, ...}}``. Source of truth for
    # introspection (``graph set schema/example/find``) and the lazy
    # flush into ``autonomy.schema#1``.
    #
    # Two declaration shapes coexist:
    #
    # 1. Direct dict assignment:
    #        _field_metadata: dict[str, dict] = {"name": {...}}
    # 2. Typed annotations + :func:`field`:
    #        name: str = field(required=True, description="...")
    #    ``__init_subclass__`` derives the dict at class creation time.
    #
    # Both forms can coexist on the same subclass; typed annotations
    # take precedence per-key.
    _field_metadata: dict[str, dict] = {}

    # Access pattern + key strategy, set by the decorators below
    # (``@append_only_log`` / ``@singleton`` / ``@keyed_per_entity`` /
    # ``@cache``). Undecorated schemas leave these as ``None`` — the
    # substrate's generic ``.write({key, payload})`` is the escape hatch.
    _access_pattern: str | None = None
    _key_strategy: str | None = None

    # ``@cache(ttl=...)``-only: TTL in whole seconds, stamped onto the
    # ``expires_at`` column on every write to a row of this schema.
    # ``None`` for non-cache schemas (they never expire and have no
    # ``expires_at`` column value).
    _cache_ttl_seconds: int | None = None

    # ``@vaulted(tier=...)``-only: the release rule for this set's payloads.
    # ``None`` means an ordinary setting, whose payload is stored as it always
    # was — see :func:`vaulted`.
    _vault_tier: str | None = None

    # ``@signer(tier)``-only: which key signs this set's organization rows.
    # ``None`` means UNDECLARED — resolution collapses that to ``delegate``
    # (:func:`declared_signer`), but the None is kept distinguishable here
    # so introspection can list a defaulted schema apart from a reviewed
    # ``@signer("delegate")`` — see :func:`signer_declaration`.
    _signer_tier: str | None = None

    #: A schema that is NOT a Setting row. Typed payload contracts borrow this
    #: class for its field metadata -- to drive TypeScript generation and to
    #: validate a JSON boundary -- without ever being stored as Settings.
    #: Marking one keeps it out of the registry, so nothing that walks the
    #: registry treats it as a Setting: not the schema-meta flush that writes
    #: every registered schema into every org database, not enforcement, not
    #: the contract checks. Inherited, so a variant of a payload contract is
    #: one too. Reach such a schema by explicit ``module:Class`` reference,
    #: which is how ``graph set typegen --schemas`` already does it.
    internal: bool = False

    # Variant discriminated-union machinery. ``_variants`` maps
    # discriminator slug → variant subclass for every direct subclass of
    # this class (one level only; multi-level hierarchies preserve their
    # tree shape rather than flattening). ``_variant_slug`` is set on a
    # subclass when ``__init_subclass__`` registers it as a variant of
    # its immediate parent — schemas that subclass ``SettingSchema``
    # directly are not variants and leave it ``None``. Codegen consumers
    # (1D and beyond) read these to produce per-variant typed methods.
    _variants: dict[str, type] = {}
    _variant_slug: str | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Each subclass owns its own variant registry — initialize a
        # fresh dict on the class so we don't accidentally share the
        # base class's mutable default across subclasses.
        cls._variants = {}
        inherited = _merged_inherited_field_metadata(cls)
        explicit = dict(cls.__dict__.get("_field_metadata", {}) or {})
        anns = cls.__dict__.get("__annotations__")
        if not anns:
            if inherited and explicit:
                merged = dict(inherited)
                merged.update(explicit)
                cls._field_metadata = merged
            _normalize_declared_remediations(cls)
            _extract_element_schemas(cls)
            _compose_set_id_from_suffix(cls)
            _register_variant(cls)
            _auto_register_schema(cls)
            _discover_actions(cls)
            return
        # Annotations may be strings under ``from __future__ import
        # annotations``. Resolve them in the defining module's namespace
        # so the JSON-schema type mapping can dispatch on real types.
        try:
            from typing import get_type_hints
            resolved = get_type_hints(cls)
        except Exception as exc:
            logger.warning(
                "get_type_hints(%s) failed; typed annotations fall back to "
                "raw strings (may resolve to type='string'): %s",
                cls.__qualname__, exc,
            )
            resolved = {}
        derived: dict[str, dict] = {}
        for name, raw_ann in anns.items():
            ann = resolved.get(name, raw_ann)
            # Skip class metadata and any private attribute.
            if name in ("set_id", "schema_revision"):
                continue
            if name.startswith("_"):
                continue
            spec = cls.__dict__.get(name)
            if not isinstance(spec, _FieldSpec):
                continue
            derived[name] = _build_metadata_from_spec(ann, spec)
            # Replace the class attribute with the actual default (or
            # remove it for required fields with no default) so that
            # the _FieldSpec doesn't leak into runtime access.
            if spec.default is not _MISSING:
                setattr(cls, name, spec.default)
            elif spec.default_factory is not None:
                setattr(cls, name, spec.default_factory())
            else:
                try:
                    delattr(cls, name)
                except AttributeError:
                    pass
        if inherited or explicit or derived:
            # Inherited metadata first, then the subclass's explicit
            # dict-form entries, then typed-derived fields as the highest
            # precedence source.
            merged = dict(inherited)
            merged.update(explicit)
            merged.update(derived)
            cls._field_metadata = merged
        _normalize_declared_remediations(cls)
        _extract_element_schemas(cls)
        _compose_set_id_from_suffix(cls)
        _register_variant(cls)
        _auto_register_schema(cls)
        _discover_actions(cls)

    @classmethod
    def validate(cls, payload: dict) -> None:
        """Raise ``SchemaValidationError`` if *payload* is invalid.

        Default implementation accepts any dict. Override per-contract.
        """
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, got {type(payload).__name__}"
            )

    @classmethod
    def export_json_schema(cls) -> dict:
        """Synthesize the meta-Setting payload for this schema.

        The output is intentionally close to draft-07 JSON Schema with
        substrate-specific extensions: ``type: object``, ``properties``
        keyed by field name, ``required`` listing required field names,
        plus ``access_pattern`` / ``key_strategy`` (decorator-driven)
        and ``variants`` (recursive map of subclass discriminator slug
        to that variant's payload).

        Per-field property dicts copy through ``description``, ``type``,
        ``enum``, ``default``, ``element`` (array element shape), and the
        data-only ``remediation`` reference.

        Variants recursively carry their own ``properties``,
        ``required``, and nested ``variants`` — variant payloads merge
        the inherited ``_field_metadata`` from their MRO (the inheritance
        fix from auto-vumin) so consumers see every variant's full
        shape without re-walking the class hierarchy themselves.

        Schemas without a populated ``_field_metadata`` produce an empty
        properties dict; schemas without variants produce ``variants:
        {}``; undecorated schemas produce ``access_pattern: None`` and
        ``key_strategy: None``.
        """
        payload = cls._export_payload()
        payload["set_id"] = cls.set_id
        payload["schema_revision"] = cls.schema_revision
        payload["access_pattern"] = cls._access_pattern
        payload["key_strategy"] = cls._key_strategy
        # Both the resolved tier and whether it was a reviewed choice: a
        # sensitive schema that FORGOT @signer("persona") signs unattended
        # and is invisible in the resolved tier alone — the export is where
        # an audit can list every defaulted schema.
        signer_tier = getattr(cls, "_signer_tier", None)
        payload["signer"] = {
            "tier": signer_tier if signer_tier is not None else "delegate",
            "explicit": signer_tier is not None,
        }
        if cls._cache_ttl_seconds is not None:
            payload["cache_ttl_seconds"] = int(cls._cache_ttl_seconds)
        return payload

    @classmethod
    def _export_payload(cls) -> dict:
        """Recursive piece of :meth:`export_json_schema`.

        Returns the per-class payload sans root-only fields (``set_id``,
        ``schema_revision``, ``access_pattern``, ``key_strategy``).
        Variants nest under ``variants[slug]`` via the same helper, so
        each variant's payload inherits the same recursion shape.
        """
        properties: dict[str, dict] = {}
        required: list[str] = []
        for name, meta in cls._field_metadata.items():
            prop: dict[str, Any] = {}
            for k in (
                "type", "description", "enum", "default", "element",
                "remediation",
            ):
                if k in meta:
                    prop[k] = meta[k]
            properties[name] = prop
            if meta.get("required"):
                required.append(name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "variants": {
                slug: variant._export_payload()
                for slug, variant in cls._variants.items()
            },
        }


# ── Storage ──────────────────────────────────────────────────


SCHEMAS: dict[str, type[SettingSchema]] = {}
# UPCONVERTERS keyed by "set_id#from->to". Always single-step (N-1 → N);
# multi-step chains are composed at lookup time.
UPCONVERTERS: dict[str, Callable[[dict], dict]] = {}


def schema_key(set_id: str, revision: int) -> str:
    """Return the joined display form: ``set_id#revision``."""
    return f"{set_id}#{revision}"


def _hop_key(set_id: str, from_rev: int, to_rev: int) -> str:
    return f"{set_id}#{from_rev}->{to_rev}"


# ── Registration ─────────────────────────────────────────────


def register_schema(
    set_id: str,
    revision: int,
    model_cls: type[SettingSchema],
    *,
    upconvert_from_prev: Callable[[dict], dict] | None = None,
) -> None:
    """Register a schema for ``(set_id, revision)``.

    If *upconvert_from_prev* is given, also register the ``rev-1 -> rev``
    hop so consumers asking for ``target_revision = revision`` can accept
    older stored rows.

    Raises ``TypeError`` when a *different* class is already registered at
    the same ``(set_id, revision)`` — the most likely trigger is two
    subclasses composing to the same key via ``set_id_suffix``. Re-
    registering the *same* class object is idempotent (so an explicit
    ``register_schema`` call after auto-registration is a no-op).
    """
    key = schema_key(set_id, revision)
    existing = SCHEMAS.get(key)
    if existing is not None and existing is not model_cls:
        raise TypeError(
            f"schema collision at {key}: already registered to "
            f"{existing.__qualname__}, refusing to overwrite with "
            f"{model_cls.__qualname__}"
        )
    SCHEMAS[key] = model_cls
    if upconvert_from_prev is not None:
        register_upconverter(set_id, revision - 1, revision, upconvert_from_prev)


def register_upconverter(
    set_id: str,
    from_rev: int,
    to_rev: int,
    fn: Callable[[dict], dict],
) -> None:
    """Register an upconverter for a single revision hop."""
    if to_rev != from_rev + 1:
        raise ValueError(
            f"upconverters must be single-step: got {from_rev} -> {to_rev}"
        )
    UPCONVERTERS[_hop_key(set_id, from_rev, to_rev)] = fn


def unregister_schema(set_id: str, revision: int) -> None:
    """Test helper: drop a registration without affecting the rest."""
    SCHEMAS.pop(schema_key(set_id, revision), None)
    # Drop adjacent upconverters too — registrations are normally a unit.
    UPCONVERTERS.pop(_hop_key(set_id, revision - 1, revision), None)
    UPCONVERTERS.pop(_hop_key(set_id, revision, revision + 1), None)


# ── Lookup ───────────────────────────────────────────────────


def get_schema(set_id: str, revision: int) -> type[SettingSchema] | None:
    """Return the registered schema class, or ``None``."""
    return SCHEMAS.get(schema_key(set_id, revision))


def list_registered_set_ids() -> list[str]:
    """Return distinct ``set_id`` values that have at least one registered
    revision.
    """
    return sorted({key.split("#", 1)[0] for key in SCHEMAS})


def upconvert_chain(
    set_id: str,
    from_rev: int,
    to_rev: int,
) -> list[Callable[[dict], dict]] | None:
    """Return the list of single-hop upconverters that take a payload from
    ``from_rev`` to ``to_rev``, or ``None`` if any hop is missing.

    Identity case (``from_rev == to_rev``) returns ``[]``. Downconversion
    (``from_rev > to_rev``) returns ``None`` — downgrades are explicit
    opt-ins, not part of the registry.
    """
    if from_rev == to_rev:
        return []
    if from_rev > to_rev:
        return None
    chain: list[Callable[[dict], dict]] = []
    for r in range(from_rev, to_rev):
        fn = UPCONVERTERS.get(_hop_key(set_id, r, r + 1))
        if fn is None:
            return None
        chain.append(fn)
    return chain


def upconvert_payload(
    set_id: str,
    from_rev: int,
    to_rev: int,
    payload: dict,
) -> dict | None:
    """Apply the registered upconvert chain. Returns the converted payload,
    or ``None`` if any hop is missing.
    """
    chain = upconvert_chain(set_id, from_rev, to_rev)
    if chain is None:
        return None
    out = payload
    for fn in chain:
        out = fn(out)
    return out


# ── Validation ───────────────────────────────────────────────


def enforce_declared_fields(schema: type, payload: Any) -> None:
    """Enforce a schema's DECLARED field metadata against *payload*.

    Runs for every schema, including those overriding :meth:`validate`, so an
    override adds contract-specific rules rather than replacing the declared
    ones. Declaring a field is what makes it enforced; without this, a schema
    could declare a field as required and never check it.

    Four rules, all read from ``_field_metadata``:

    * a payload key the schema does not declare is rejected — the schema is
      the complete statement of the payload's shape, which is what makes an
      aliased or misspelled field a failure instead of a silently ignored key;
    * a required field that is absent, or present as ``None``, is missing;
    * a declared type is checked, but only for a non-``None`` value;
    * a declared enum is checked, likewise only for a non-``None`` value;
    * a declared ``max_length`` is checked on a string, likewise.

    ``max_length`` is here rather than in any one schema because a length
    bound is a fact about a field, and ``_field_metadata`` is what
    ``graph set schema`` and the dashboard's schema route read. A cap
    enforced inside a single schema's own validator holds, but is invisible
    to both, so nothing renders it and no other schema can declare it.

    ``None`` is permitted for an optional field rather than treated as a type
    error. That is the convention the hand-written validators already follow
    independently — the credential schemas type-check with
    ``v is not None and not isinstance(...)``, and the harness-usage schema
    reads ``None`` on a required field as absent.

    A schema declaring no fields enforces nothing here, so an undeclared
    contract stays as permissive as it is today rather than rejecting
    everything.
    """
    meta = getattr(schema, "_field_metadata", None) or {}
    if not meta:
        return
    if not isinstance(payload, dict):
        raise SchemaValidationError(
            f"{schema.__name__}: payload must be a dict, "
            f"got {type(payload).__name__}"
        )
    undeclared = sorted(set(payload) - set(meta))
    if undeclared:
        raise SchemaValidationError(
            f"{schema.__name__}: undeclared field(s): {undeclared}"
        )
    for name, spec in meta.items():
        present = name in payload
        value = payload.get(name)
        if spec.get("required") and (not present or value is None):
            raise SchemaValidationError(
                f"{schema.__name__}: missing required field {name!r}"
            )
        if not present or value is None:
            continue
        element = (getattr(schema, "_element_schemas", None) or {}).get(name)
        if element is not None and isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    raise SchemaValidationError(
                        f"{schema.__name__}: {name}[{index}] must be an object, "
                        f"got {type(item).__name__}"
                    )
                try:
                    enforce_declared_fields(element, item)
                except SchemaValidationError as exc:
                    raise SchemaValidationError(
                        f"{schema.__name__}: {name}[{index}] {exc}"
                    ) from None

        want = _JSON_TYPE_TO_PY.get(spec.get("type"))
        if want is not None and not isinstance(value, want):
            raise SchemaValidationError(
                f"{schema.__name__}: {name!r} must be {spec['type']}, "
                f"got {type(value).__name__}"
            )
        enum = spec.get("enum")
        if enum and value not in enum:
            raise SchemaValidationError(
                f"{schema.__name__}: {name!r} must be one of {enum}, "
                f"got {value!r}"
            )
        cap = spec.get("max_length")
        if cap and isinstance(value, str) and len(value) > cap:
            raise SchemaValidationError(
                f"{schema.__name__}: {name!r} is {len(value)} characters, "
                f"over the {cap} this field allows"
            )


_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def validate_key(set_id: str, revision: int, key: str) -> None:
    """Check *key* against the schema's declared key strategy.

    Only the strategies that state a FORM are checkable: ``fixed:X`` means
    the key is literally ``X``, and ``uuid_v4`` means it is a uuid. The rest
    name the ENTITY the key identifies — ``org_slug``, ``workspace_id``,
    ``session_name:participant_id`` — which says what the key means rather
    than what it looks like, and is for a reader, not a matcher.

    The point of the checkable half is the false-plurality class: a schema
    declaring one fixed row while a writer quietly uses a second key is how
    a singleton becomes a set nobody designed, and how readers end up
    scanning for the row they want.

    Unknown schema is left alone here; :func:`validate_payload` reports it.
    """
    schema = get_schema(set_id, revision)
    if schema is None:
        return
    strategy = getattr(schema, "_key_strategy", None)
    if not strategy:
        return
    if strategy.startswith("fixed:"):
        expected = strategy.split(":", 1)[1]
        if key != expected:
            raise SchemaValidationError(
                f"{schema.__name__}: declares a single row keyed "
                f"{expected!r}, so it cannot also be written at {key!r}. "
                f"Either this is a second entity — in which case the schema "
                f"is not a singleton — or the key is wrong."
            )
    elif strategy == "uuid_v4" and not _UUID4_RE.match(key or ""):
        raise SchemaValidationError(
            f"{schema.__name__}: keys are generated uuids, got {key!r}"
        )


def validate_payload(set_id: str, revision: int, payload: Any) -> None:
    """Validate *payload* against ``(set_id, revision)``.

    Both the schema's own :meth:`validate` and its declared field metadata
    must accept the payload. The schema's method runs FIRST so that where
    both would reject, the caller sees the contract-specific message —
    "kind=choice requires a non-empty 'choice'" rather than a generic
    missing-field notice. Declared enforcement then catches what the
    method did not check, which for a schema that never overrode
    :meth:`validate` is everything.

    Raises ``SchemaValidationError`` if the schema is registered and rejects
    the payload, or if the schema is unknown.
    """
    schema = get_schema(set_id, revision)
    if schema is None:
        raise SchemaValidationError(
            f"unknown schema: {schema_key(set_id, revision)}"
        )
    schema.validate(payload)
    enforce_declared_fields(schema, payload)


# ── Schema-as-Setting flush (auto-82xyq) ─────────────────────


SCHEMA_META_SET_ID = "autonomy.schema"
SCHEMA_META_REVISION = 1
SYNOPSIS_META_SET_ID = "autonomy.schema.synopsis"
SYNOPSIS_META_REVISION = 1


def _module_synopsis(model_cls: type[SettingSchema]) -> dict | None:
    """Return the ``SYNOPSIS`` constant from *model_cls*'s defining module,
    or ``None`` if the module didn't declare one.
    """
    mod = sys.modules.get(model_cls.__module__)
    if mod is None:
        return None
    syn = getattr(mod, "SYNOPSIS", None)
    if not isinstance(syn, dict):
        return None
    return syn


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cache_expires_at(set_id: str, revision: int, now_iso: str) -> str | None:
    """Return ISO-8601 ``expires_at`` for cache schemas; ``None`` otherwise.

    Cache schemas are those decorated with :func:`cache` — the decorator
    stamps ``_cache_ttl_seconds`` on the class. For every other schema
    (and unregistered ``(set_id, revision)`` pairs) this returns
    ``None``, which writers stamp into the column as ``NULL`` so the
    GC sweep skips the row.

    The output format matches existing ``created_at`` / ``updated_at``
    timestamps: zero-padded ISO-8601 UTC with second resolution and a
    trailing ``Z``. ``now_iso`` is the same string the caller is about
    to write into ``updated_at`` — passing the canonical ``_now_iso()``
    output keeps the substrate's idea of "now" consistent across
    columns within a single write.
    """
    schema = get_schema(set_id, revision)
    if schema is None:
        return None
    ttl_seconds = getattr(schema, "_cache_ttl_seconds", None)
    if not ttl_seconds:
        return None
    now_dt = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc,
    )
    return (now_dt + timedelta(seconds=int(ttl_seconds))).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


def _upsert_meta_setting(
    db,
    *,
    meta_set_id: str,
    meta_revision: int,
    key: str,
    payload: dict,
    now: str,
) -> None:
    """Idempotent upsert: one base Setting row per ``(meta_set_id, key)``.

    Multiple base rows would be a registry/flush bug; the first one wins
    and we update its payload in place. We deliberately skip
    ``schemas.validate_payload`` — the meta set_ids are not registered as
    SettingSchemas, and their payloads are produced by code we control.
    """
    serialized = json.dumps(payload, sort_keys=True)
    row = db.conn.execute(
        "SELECT id, payload FROM settings "
        "WHERE set_id = ? AND key = ? "
        "  AND supersedes IS NULL AND excludes IS NULL",
        (meta_set_id, key),
    ).fetchone()
    if row is not None:
        if row["payload"] != serialized:
            db.conn.execute(
                "UPDATE settings SET payload = ?, updated_at = ? WHERE id = ?",
                (serialized, now, row["id"]),
            )
        return
    db.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid4()), meta_set_id, int(meta_revision), key, serialized,
         "canonical", now, now),
    )


def flush_schema_meta(db) -> None:
    """Idempotently upsert schema + synopsis meta-Settings into ``db``.

    The flush walks the live ``SCHEMAS`` registry rather than a separate
    pending-upsert list — every registration is the source of truth, and
    re-registering simply re-flushes. Read-only DBs are skipped.
    Subsequent calls against an unchanged registry no-op (payload-equality
    short-circuits the UPDATE).

    Writes both meta-row families: ``autonomy.schema#1`` (from
    ``export_json_schema()``) and ``autonomy.schema.synopsis#1`` (from the
    defining module's ``SYNOPSIS`` dict). Editing only a synopsis therefore
    lands here too — that route is what ``graph set find`` ranks on.

    Invoked once, against the machine store, at dashboard startup via
    :func:`flush_schema_meta_machine_store`; it costs roughly two idempotent
    SELECT lookups per registered schema (~70+ on the current registry, and
    growing with every schema added), which is why it runs once at startup
    rather than on every writable open.
    """
    if getattr(db, "read_only", False):
        return
    if not SCHEMAS:
        return
    now = _now_iso()
    for sk, model_cls in SCHEMAS.items():
        try:
            json_schema = model_cls.export_json_schema()
        except Exception:
            # Defensive: a schema with a broken export shouldn't crash
            # every DB connection. Skip it; ``set schema`` will surface
            # the absence on lookup.
            continue
        _upsert_meta_setting(
            db,
            meta_set_id=SCHEMA_META_SET_ID,
            meta_revision=SCHEMA_META_REVISION,
            key=sk,
            payload=json_schema,
            now=now,
        )
        synopsis = _module_synopsis(model_cls)
        if synopsis is not None:
            _upsert_meta_setting(
                db,
                meta_set_id=SYNOPSIS_META_SET_ID,
                meta_revision=SYNOPSIS_META_REVISION,
                key=sk,
                payload=synopsis,
                now=now,
            )
    db.conn.commit()


def flush_schema_meta_machine_store(*, root=None) -> int:
    """Materialize schema + synopsis meta-Settings in the MACHINE STORE.

    Called once at dashboard startup (see :mod:`tools.dashboard.server`).
    The dashboard hot-reloads — i.e. restarts the process — on every code
    change, and the schema registry can only change when code loads, so a
    single startup flush is sufficient to make schema/synopsis edits live
    with **no** ``_SCHEMA_USER_VERSION`` bump and **no** full table
    re-init. Deliberately not wired into ``GraphDB._init_schema``; that
    coupling was the bug fixed in auto-06ziz.

    ONE store, not one per organization (auto-n77vh). Schema metadata is a
    projection of the code THIS PROCESS is running: no member authors it,
    nothing can sign it, and two machines on different code versions have
    no single true answer per organization — flushing it into shared org
    databases made whoever restarted last win, and was the one unsigned
    ingress the signed-settings design could not cover. The machine store
    is resolved BY NAME (never by constructing an org-namespace path —
    auto-35kmy moved it beside ``data/orgs/``), created on demand, and its
    ``canonical`` rows participate in every organization's read on this
    machine because ``resolve_peers`` names the operator's own stores
    EXPLICITLY and unconditionally (auto-9uj7i) — a peer subscription
    governs OTHER organizations only and cannot remove them, so a pinned
    org resolves schemas identically to an unpinned one.

    Also sweeps the rows this flush historically wrote into organization
    databases and the personal store, so exactly one copy exists per
    machine. Returns 1 on a successful machine-store flush, 0 otherwise.
    """
    # Imported lazily to avoid an import cycle: db -> schemas (settings_ops)
    # -> back into db. org_ops likewise pulls in db.
    from ..db import GraphDB, _org_db_path

    try:
        path = _org_db_path("machine", root)
        if path.exists():
            db = GraphDB(path)
        else:
            # First creation writes the typed bootstrap row, not a bare
            # file: list_orgs is the operator's store inventory and skips
            # files without one, so a bare store would exist, hold the
            # whole registry, serve reads — and report as absent. Local
            # stores carry type='personal'; the slug tells them apart.
            try:
                db = GraphDB.create_org_db(
                    "machine", type_="personal", root=root,
                )
            except FileExistsError:
                # Lost a concurrent-creation race; the winner's file is
                # there now.
                db = GraphDB(path)
    except Exception:
        logger.warning(
            "flush_schema_meta_machine_store: could not open the machine "
            "store; schema metadata not materialized", exc_info=True,
        )
        return 0
    try:
        flush_schema_meta(db)
        flushed = 1
    except Exception:
        logger.warning(
            "flush_schema_meta_machine_store: flush failed", exc_info=True,
        )
        flushed = 0
    finally:
        db.close()
    _sweep_org_schema_meta(root=root)
    return flushed


def _sweep_org_schema_meta(*, root=None) -> int:
    """One-time removal of schema metadata from org DBs and personal.

    Machine-projection rows have exactly one correct home; a copy left in
    a shared database keeps the whoever-restarted-last fight alive and is
    an unsigned row where every row must be signed. Hard delete — these
    are startup-refreshed cache rows, not authored content. Idempotent and
    cheap once clean. Returns rows removed.
    """
    from ..db import GraphDB
    from .. import org_ops

    removed = 0
    targets = [ref.slug for ref in org_ops.list_orgs(root=root)]
    for slug in targets:
        if slug == "machine":
            continue  # the one correct home
        try:
            db = GraphDB.open_org_db(slug, mode="rw", root=root)
        except Exception:
            logger.warning(
                "_sweep_org_schema_meta: could not open %r; skipping",
                slug, exc_info=True,
            )
            continue
        try:
            cur = db.conn.execute(
                "DELETE FROM settings WHERE set_id IN (?, ?)",
                (SCHEMA_META_SET_ID, SYNOPSIS_META_SET_ID),
            )
            if cur.rowcount:
                removed += cur.rowcount
                logger.info(
                    "_sweep_org_schema_meta: removed %d schema-meta row(s) "
                    "from %r", cur.rowcount, slug,
                )
            db.conn.commit()
        except Exception:
            logger.warning(
                "_sweep_org_schema_meta: sweep failed for %r; skipping",
                slug, exc_info=True,
            )
        finally:
            db.close()
    return removed
