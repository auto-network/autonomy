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
``flush_schema_meta_all_orgs()`` walks every per-org DB and flushes each,
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
import re
import sys
from dataclasses import dataclass
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


def field(
    *,
    required: bool | None = None,
    default: Any = _MISSING,
    default_factory: Callable[[], Any] | None = None,
    description: str | None = None,
    enum: list | None = None,
    element: Any = None,
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
            describes a list-of-dict element shape.

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
    )


def _normalize_element(element: Any) -> Any:
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
        target._access_pattern = "append_only_log"
        target._key_strategy = key_name
        return target

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
        target._access_pattern = "singleton"
        target._key_strategy = f"fixed:{key}"
        return target

    if cls is None:
        return _wrap
    return _wrap(cls)


def keyed_per_entity(
    cls: type | None = None,
    *,
    key_strategy: str = "natural",
) -> Any:
    """Schema decorator: declare per-entity rows with caller-supplied keys.

    Codegen-aware consumers expose ``.upsert(key, payload)`` instead of
    generic ``.write({key, payload})``. ``key_strategy`` names the
    caller-side convention (``natural`` = caller picks; future
    strategies can name structured-key derivations).

    Usable as ``@keyed_per_entity`` (bare) or
    ``@keyed_per_entity(key_strategy="...")``.
    """
    def _wrap(target: type) -> type:
        target._access_pattern = "keyed_per_entity"
        target._key_strategy = key_strategy
        return target

    if cls is None:
        return _wrap
    return _wrap(cls)


def cache(
    cls: type | None = None,
    *,
    ttl: timedelta,
    key_strategy: str = "natural",
) -> Any:
    """Schema decorator: declare cache semantics with TTL-driven GC.

    A cache schema's rows carry an absolute ``expires_at`` set at
    write time (``updated_at + ttl``). The cache_gc cron sweeps rows
    whose expires_at has elapsed and whose publication_state is
    below ``published``.

    ``ttl`` is required — there is no default-eternal cache.
    ``timedelta`` is enforced (not int seconds) so the unit is
    explicit at the call site. ``key_strategy`` mirrors
    :func:`keyed_per_entity` since cache rows are caller-keyed
    upserts.

    Usable only with parens: ``@cache(ttl=timedelta(...))``.
    """
    if not isinstance(ttl, timedelta):
        raise TypeError("@cache requires ttl=timedelta(...)")
    seconds = int(ttl.total_seconds())
    if seconds <= 0:
        raise ValueError("@cache requires a positive ttl")

    def _wrap(target: type) -> type:
        target._access_pattern = "cache"
        target._key_strategy = key_strategy
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
        ``enum``, ``default``, and ``element`` (array element shape).

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
            for k in ("type", "description", "enum", "default", "element"):
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
    * a declared enum is checked, likewise only for a non-``None`` value.

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

    Invoked once per org at dashboard startup via
    :func:`flush_schema_meta_all_orgs`; it costs roughly two idempotent
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


def flush_schema_meta_all_orgs(*, root=None) -> int:
    """Flush schema + synopsis meta-Settings into every per-org DB.

    Called once at dashboard startup (see :mod:`tools.dashboard.server`).
    The dashboard hot-reloads — i.e. restarts the process — on every code
    change, and the schema registry can only change when code loads, so a
    single startup flush is sufficient to make schema/synopsis edits live
    with **no** ``_SCHEMA_USER_VERSION`` bump and **no** full table
    re-init. Deliberately not wired into ``GraphDB._init_schema``; that
    coupling was the bug fixed in auto-06ziz.

    Each org is opened writably and flushed via :func:`flush_schema_meta`,
    which is idempotent (an unchanged registry re-flushes to no-ops).
    Read-only / broken org DBs are skipped rather than raised. Returns the
    number of DBs successfully flushed.

    Note: an org DB that is only ever opened read-only never materializes
    its own rows under any design. Cross-org schema discovery does not
    depend on per-org materialization — schema-meta rows are written
    ``publication_state="canonical"`` and every org resolves platform
    schemas through peer merge from ``autonomy``. That holds only while an
    org subscribes to ``autonomy`` as a peer; an org pinning a narrower
    ``autonomy.org.peer-subscription`` would lose platform-schema discovery
    entirely, and this flush would not restore it.
    """
    # Imported lazily to avoid an import cycle: db -> schemas (settings_ops)
    # -> back into db. org_ops likewise pulls in db.
    from ..db import GraphDB
    from .. import org_ops

    flushed = 0
    for ref in org_ops.list_orgs(root=root):
        try:
            db = GraphDB.open_org_db(ref.slug, mode="rw", root=root)
        except Exception:
            logger.warning(
                "flush_schema_meta_all_orgs: could not open org %r; skipping",
                ref.slug, exc_info=True,
            )
            continue
        try:
            flush_schema_meta(db)
            flushed += 1
        except Exception:
            logger.warning(
                "flush_schema_meta_all_orgs: flush failed for org %r; skipping",
                ref.slug, exc_info=True,
            )
        finally:
            db.close()
    return flushed
