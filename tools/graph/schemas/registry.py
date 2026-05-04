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

Schema-as-Setting (auto-82xyq): registered schemas are also lazily
upserted into each per-org DB as ``autonomy.schema#1`` Settings, with
their hand-curated synopsis surfacing as ``autonomy.schema.synopsis#1``.
``flush_schema_meta(db)`` performs the upsert; ``GraphDB._init_schema``
calls it once per writable connection. The flush is idempotent — payload
match is a no-op.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
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


_PY_TO_JSON_TYPE = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    list: "array",
    dict: "object",
}


def _python_type_to_json_type(t: Any) -> str:
    """Map a Python type (or tuple of types) to a JSON-schema type name."""
    if isinstance(t, tuple):
        for cand in t:
            mapped = _PY_TO_JSON_TYPE.get(cand)
            if mapped is not None:
                return mapped
        return "string"
    return _PY_TO_JSON_TYPE.get(t, "string")


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
            _register_variant(cls)
            _auto_register_schema(cls)
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
        _register_variant(cls)
        _auto_register_schema(cls)

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
    """
    key = schema_key(set_id, revision)
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


def validate_payload(set_id: str, revision: int, payload: Any) -> None:
    """Validate *payload* against ``(set_id, revision)``.

    Raises ``SchemaValidationError`` if the schema is registered and rejects
    the payload, or if the schema is unknown.
    """
    schema = get_schema(set_id, revision)
    if schema is None:
        raise SchemaValidationError(
            f"unknown schema: {schema_key(set_id, revision)}"
        )
    schema.validate(payload)


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

    Called once per writable :class:`GraphDB` connection from
    ``GraphDB._init_schema``. The cost (~24 SELECT no-ops on a populated
    DB) is dominated by the surrounding migration pass.
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
