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
import sys
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4


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

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        anns = cls.__dict__.get("__annotations__")
        if not anns:
            return
        # Annotations may be strings under ``from __future__ import
        # annotations``. Resolve them in the defining module's namespace
        # so the JSON-schema type mapping can dispatch on real types.
        try:
            from typing import get_type_hints
            resolved = get_type_hints(cls)
        except Exception:
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
        if derived:
            # Existing dict-form _field_metadata first, derived overrides.
            base = dict(cls.__dict__.get("_field_metadata", {}) or {})
            base.update(derived)
            cls._field_metadata = base

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
        """Synthesize a json-schema-compatible dict from ``_field_metadata``.

        The output shape is intentionally close to draft-07: ``type:
        object``, ``properties`` keyed by field name, ``required``
        listing field names whose metadata flags ``required: True``.
        Per-field property dicts copy through ``description``, ``type``,
        ``enum``, ``default``, and ``element`` (array element shape).

        Schemas without a populated ``_field_metadata`` produce an empty
        properties dict — useful for the test-only stub schemas that
        exist only to satisfy the registry contract.
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
            "set_id": cls.set_id,
            "schema_revision": cls.schema_revision,
            "type": "object",
            "properties": properties,
            "required": required,
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
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
