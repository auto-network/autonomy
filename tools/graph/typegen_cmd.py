"""``graph set typegen`` — schema-derived TypeScript codegen.

Walks a plugin's ``entrypoints.schemas``, resolves each ``module:Class``
entry to its ``SettingSchema`` subclass, and emits a TypeScript
declaration file describing every Setting payload the plugin reads or
writes. Output lands at ``tools/dashboard/static/js/types/<id>.d.ts``
by default. ``--stdout`` prints to stdout, ``--out PATH`` writes to a
custom destination, and ``--check`` exits non-zero if the on-disk file
diverges from the freshly generated content (the drift gate that
pre-commit / CI hooks call into).

Generation is a pure function over the plugin's manifest + each
schema's ``export_json_schema()`` payload — no Setting reads, no
network. A schema without variants yields an interface; variant-bearing
schemas yield a base interface plus per-variant interfaces and a
top-level discriminated-union ``type``.

Spec: ``graph://865295b3-5cc`` § Generated TypeScript and JSDoc.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Plugin discovery is owned by ``tools.dashboard.plugin_api.loader``. We
# import lazily inside callers that need it so unit tests can stub the
# discovery surface without dragging dashboard imports into every CLI
# invocation.


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tools" / "dashboard" / "static" / "js" / "types"


# ── Type rendering ────────────────────────────────────────────


def _ts_type(prop: dict[str, Any]) -> str:
    """Map a JSON-schema property dict to a TypeScript type expression.

    Handles the substrate's exported shape: ``type`` (string/integer/
    number/boolean/array/object), optional ``enum`` (union of literals),
    optional ``element`` (per-element shape for arrays — bare type
    string or an object-shape map). Falls back to ``unknown`` for
    anything we can't map.
    """
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(_literal(v) for v in enum)
    t = prop.get("type", "string")
    if t == "string":
        return "string"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "array":
        element = prop.get("element")
        return f"{_ts_array_element(element)}[]"
    if t == "object":
        return "Record<string, unknown>"
    return "unknown"


def _ts_array_element(element: Any) -> str:
    """Render an array's element shape as a TypeScript type expression.

    * ``None`` / unrecognized → ``unknown``
    * Bare type-name string (``"string"``) → ``string``
    * ``{"type": "string"}`` style → render via :func:`_ts_type`
    * ``{"name": <type-name>, ...}`` (legacy element-of-types map) →
      object literal
    * ``{"name": {"type": ..., "required": ...}}`` (rich element-spec
      map, as used by ``capability_impl#1.implements`` and
      ``capability_contract#1.ops``) → object literal with optional/
      required markers driven by per-field ``required`` flags.
    """
    if element is None:
        return "unknown"
    if isinstance(element, str):
        return _ts_type({"type": element})
    if isinstance(element, dict):
        # Single-prop dict-of-spec-dict (`{"type": "string"}`) — treat
        # as a property descriptor rather than an object shape.
        if set(element).issubset({"type", "enum", "element"}):
            return _ts_type(element)
        # Object-shape map: per-key descriptor or bare type name.
        parts: list[str] = []
        for key, sub in element.items():
            if isinstance(sub, dict):
                ts = _ts_type(sub)
                required = bool(sub.get("required"))
            else:
                ts = _ts_type({"type": sub})
                required = True
            sep = ":" if required else "?:"
            parts.append(f"{key}{sep} {ts}")
        return "{ " + "; ".join(parts) + " }"
    return "unknown"


def _literal(value: Any) -> str:
    """Render a Python value as a TypeScript literal type."""
    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return f"'{value!s}'"


# ── Interface / union rendering ──────────────────────────────


@dataclass(frozen=True)
class SchemaSource:
    """A schema entry resolved from a plugin manifest.

    ``ref`` is the ``module:ClassName`` string from the manifest; we
    keep it for the file header so consumers can grep back to the
    source declaration.
    """
    ref: str
    cls_name: str
    payload: dict


def _render_field(name: str, prop: dict[str, Any], required: bool) -> str:
    description = prop.get("description")
    sep = ":" if required else "?:"
    ts = _ts_type(prop)
    if description:
        return (
            f"  /** {description} */\n"
            f"  {name}{sep} {ts};"
        )
    return f"  {name}{sep} {ts};"


def _render_interface(
    name: str,
    payload: dict[str, Any],
    *,
    discriminator_slug: str | None = None,
    extends: str | None = None,
) -> str:
    """Render a single TypeScript interface from an exported schema payload.

    ``discriminator_slug`` injects a ``kind: 'slug'`` literal field on
    variant interfaces. ``extends`` lets variants reference their base.
    """
    properties = payload.get("properties") or {}
    required = set(payload.get("required") or [])
    extends_clause = f" extends {extends}" if extends else ""
    lines = [f"export interface {name}{extends_clause} {{"]
    if discriminator_slug is not None:
        lines.append(f"  kind: '{discriminator_slug}';")
    for fname, prop in properties.items():
        lines.append(_render_field(fname, prop, fname in required))
    lines.append("}")
    return "\n".join(lines)


def _render_schema_block(name: str, payload: dict[str, Any]) -> str:
    """Render the full block (header + interface(s) + union if variants).

    For non-variant schemas: a single interface.

    For variant schemas: a ``<Name>Base`` interface for the parent's
    fields, one ``<Name><PascalPath>`` interface per variant at every
    depth of the tree (each carrying ``kind: '<own_slug>'`` and
    extending the base), and a discriminated-union ``type <Name> = ...``
    enumerating every variant in the tree.

    Nested variants matter because the substrate's 1C tree machinery
    (`_register_variant`) preserves the full tree shape — capability
    layer namespaces (e.g. ``SourceControl > Review > {ReviewRead,
    ReviewRefresh}``, ``SourceControl > Gates > GatesSnapshot``) need
    every leaf in the union for downstream consumers (Phase 3D) to
    narrow on the leaf discriminator. The walker below does a DFS
    over ``variants`` (and ``variants[*]['variants']`` recursively),
    yielding one entry per visited variant.

    All variant interfaces ``extend`` the same ``<Name>Base`` rather
    than chaining through their parent variant. Chaining would clash
    on the ``kind`` discriminator literal (a parent's ``kind: 'review'``
    cannot be narrowed to ``kind: 'review_read'`` via plain extension);
    flattening to ``Base`` lets every variant declare its own
    discriminator unambiguously while the naming convention
    (``SourceControlReviewRead``) preserves the tree shape visually.
    Inherited fields are already merged into each variant's
    ``properties`` by the substrate's ``_field_metadata`` derivation,
    so each interface still carries every applicable field.
    """
    set_id = payload.get("set_id", "")
    revision = payload.get("schema_revision", "")
    access = payload.get("access_pattern")
    key_strategy = payload.get("key_strategy")
    header_lines = [f" * {set_id}#{revision}"]
    if access:
        header_lines.append(
            f" * Access pattern: {access}"
            + (f" (key strategy: {key_strategy})" if key_strategy else "")
        )
    header = "/**\n" + "\n".join(header_lines) + "\n */"

    variants = payload.get("variants") or {}
    if not variants:
        return f"{header}\n{_render_interface(name, payload)}"

    base_name = f"{name}Base"
    pieces = [header, _render_interface(base_name, payload)]
    union_members: list[str] = []
    for variant_name, variant_payload, variant_slug in _walk_variants(name, variants):
        pieces.append(
            _render_interface(
                variant_name,
                variant_payload,
                discriminator_slug=variant_slug,
                extends=base_name,
            )
        )
        union_members.append(variant_name)
    pieces.append(
        f"export type {name} =\n  | "
        + "\n  | ".join(union_members)
        + ";"
    )
    return "\n\n".join(pieces)


def _walk_variants(prefix: str, variants: dict[str, dict]):
    """DFS through a variant tree, yielding one entry per visited variant.

    *prefix* is the TS name of the parent class (the schema root or a
    parent variant); each visited variant's interface name concatenates
    the parent prefix with the variant slug rendered in PascalCase.
    Nested variants extend that path further, so a tree
    ``SourceControl > Review > ReviewRead`` produces
    ``SourceControlReview`` then ``SourceControlReviewReviewRead``
    — and to avoid stutter, we only append the variant's own
    PascalCase slug, never re-stuttering the parent path.

    Yields ``(interface_name, payload, slug)`` for each variant. The
    ``slug`` is the variant's own discriminator (e.g. ``review_read``);
    the substrate guarantees these are globally unique within a tree
    because the variant slug is derived from the class name and class
    names within a hierarchy don't collide.
    """
    for slug, variant_payload in variants.items():
        interface_name = f"{prefix}{_pascal(slug)}"
        yield interface_name, variant_payload, slug
        nested = variant_payload.get("variants") or {}
        if nested:
            yield from _walk_variants(interface_name, nested)


def _pascal(slug: str) -> str:
    """Convert a snake_case slug to PascalCase."""
    return "".join(part.capitalize() for part in slug.split("_") if part)


# ── Document assembly ────────────────────────────────────────


HEADER_TEMPLATE = """\
// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Generated by `{regen_command}`.
// To regenerate, run that command. The pre-commit drift check
// (`{regen_command} --check`) fails CI if this file diverges from the
// live Python schema declarations.
//
// Source schemas:
{source_lines}
"""


def render_dts(
    name: str,
    sources: Iterable[SchemaSource],
    *,
    regen_command: str | None = None,
) -> str:
    """Render the full ``.d.ts`` body for a set of schemas.

    Pure function: no Setting reads, no I/O. Order of *sources* is
    preserved in the output so schemas can be sorted deterministically
    by the caller.

    *regen_command* is the exact CLI invocation the operator should run
    to regenerate this file. Defaults to ``graph set typegen --plugin
    <name>`` for plugin-mode use; ``--schemas`` mode passes its own
    longer form so the header doesn't claim a plugin invocation that
    won't resolve.
    """
    sources = list(sources)
    if not sources:
        source_lines = "//   (no schemas declared)"
    else:
        source_lines = "\n".join(f"//   - {s.ref}" for s in sources)
    if regen_command is None:
        regen_command = f"graph set typegen --plugin {name}"
    header = HEADER_TEMPLATE.format(
        regen_command=regen_command, source_lines=source_lines,
    )
    blocks = [_render_schema_block(s.cls_name, s.payload) for s in sources]
    body = "\n\n".join(blocks) if blocks else ""
    if body:
        return header + "\n" + body + "\n"
    return header


# ── Plugin / schema resolution ───────────────────────────────


def _resolve_schema_entry(ref: str) -> SchemaSource:
    """Import a ``module:ClassName`` ref and call ``export_json_schema``.

    Errors raise :class:`TypegenError` with a message naming the ref so
    the operator can edit the manifest.
    """
    if ":" not in ref:
        raise TypegenError(
            f"schema entry {ref!r} must be 'module:ClassName' (got no ':')"
        )
    mod_path, _, attr = ref.partition(":")
    try:
        mod = importlib.import_module(mod_path)
    except ImportError as exc:
        raise TypegenError(
            f"schema entry {ref!r}: cannot import module {mod_path!r}: {exc}"
        ) from exc
    cls = getattr(mod, attr, None)
    if cls is None:
        raise TypegenError(
            f"schema entry {ref!r}: module {mod_path!r} has no attribute {attr!r}"
        )
    if not hasattr(cls, "export_json_schema"):
        raise TypegenError(
            f"schema entry {ref!r}: {attr!r} is not a SettingSchema subclass "
            f"(missing export_json_schema())"
        )
    payload = cls.export_json_schema()
    return SchemaSource(ref=ref, cls_name=attr, payload=payload)


class TypegenError(Exception):
    """Raised when typegen cannot resolve a schema, plugin, or output."""


# ── CLI plumbing ─────────────────────────────────────────────


def _discover_plugin(plugin_id: str):
    """Resolve a plugin id to its DiscoveredPlugin record.

    Lazy import so unit tests can avoid the dashboard import chain when
    they exercise the rendering core directly.
    """
    from tools.dashboard.plugin_api.loader import discover

    for cand in discover():
        if cand.manifest.id == plugin_id:
            return cand
    raise TypegenError(
        f"plugin {plugin_id!r} not found under tools/dashboard/plugins"
    )


def _discover_all_plugins():
    from tools.dashboard.plugin_api.loader import discover

    return list(discover())


def _resolve_plugin_sources(plugin) -> list[SchemaSource]:
    refs = (plugin.manifest.entrypoints.schemas or [])
    return [_resolve_schema_entry(ref) for ref in refs]


def _output_path(plugin_id: str, override: str | None) -> Path:
    if override is not None:
        return Path(override)
    return DEFAULT_OUTPUT_ROOT / f"{plugin_id}.d.ts"


def _write_or_check(
    *,
    plugin_id: str,
    rendered: str,
    out_path: Path,
    check: bool,
    stdout: bool,
) -> int:
    """Materialize *rendered* — write, check, or print. Returns exit code.

    * ``stdout=True`` prints the body and returns 0 (no I/O).
    * ``check=True`` reads the on-disk file (if any) and returns 1 when
      it differs from *rendered*; the diagnostic goes to stderr.
    * Otherwise writes *rendered* to *out_path* (creating parent dirs).
    """
    if stdout:
        sys.stdout.write(rendered)
        return 0
    if check:
        existing: str | None = None
        if out_path.exists():
            existing = out_path.read_text()
        if existing == rendered:
            return 0
        if existing is None:
            sys.stderr.write(
                f"typegen drift: {out_path} does not exist; "
                f"run `graph set typegen --plugin {plugin_id}` to create it.\n"
            )
        else:
            sys.stderr.write(
                f"typegen drift: {out_path} is out of date; "
                f"run `graph set typegen --plugin {plugin_id}` to refresh.\n"
            )
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered)
    return 0


def cmd_set_typegen(args) -> None:
    """Entry point for ``graph set typegen``.

    Three modes (mutually exclusive):

    * ``--plugin <id>`` walks the named plugin's manifest.
    * ``--all`` iterates every discovered plugin.
    * ``--schemas REF[,REF...] --name NAME`` generates from an arbitrary
      list of ``module:Class`` refs — used for capability-layer schemas
      and other non-plugin codegen sources. ``NAME`` becomes the
      generated file's stem (``tools/dashboard/static/js/types/<NAME>
      .d.ts``) and the header label.
    """
    if args.schemas:
        if not args.name:
            sys.stderr.write(
                "typegen: --schemas requires --name <label> for the output file stem\n"
            )
            sys.exit(2)
        refs = _parse_schemas_arg(args.schemas)
        try:
            sources = [_resolve_schema_entry(ref) for ref in refs]
        except TypegenError as exc:
            sys.stderr.write(f"typegen: {exc}\n")
            sys.exit(1)
        regen = (
            f"graph set typegen --schemas {','.join(refs)} --name {args.name}"
        )
        rendered = render_dts(args.name, sources, regen_command=regen)
        out_path = _output_path(args.name, args.out)
        sys.exit(_write_or_check(
            plugin_id=args.name,
            rendered=rendered,
            out_path=out_path,
            check=args.check,
            stdout=args.stdout,
        ))

    if args.all:
        plugins = _discover_all_plugins()
        if not plugins:
            sys.stderr.write(
                "typegen: no plugins discovered under tools/dashboard/plugins\n"
            )
            sys.exit(0)
        exit_code = 0
        for plugin in plugins:
            try:
                sources = _resolve_plugin_sources(plugin)
            except TypegenError as exc:
                sys.stderr.write(
                    f"typegen: skipping {plugin.manifest.id!r}: {exc}\n"
                )
                exit_code = max(exit_code, 1)
                continue
            rendered = render_dts(plugin.manifest.id, sources)
            out_path = _output_path(plugin.manifest.id, None)
            rc = _write_or_check(
                plugin_id=plugin.manifest.id,
                rendered=rendered,
                out_path=out_path,
                check=args.check,
                stdout=args.stdout,
            )
            exit_code = max(exit_code, rc)
        sys.exit(exit_code)

    if not args.plugin:
        sys.stderr.write(
            "typegen: --plugin <id>, --all, or --schemas REF[,REF...] --name <label> required\n"
        )
        sys.exit(2)

    try:
        plugin = _discover_plugin(args.plugin)
        sources = _resolve_plugin_sources(plugin)
    except TypegenError as exc:
        sys.stderr.write(f"typegen: {exc}\n")
        sys.exit(1)

    rendered = render_dts(plugin.manifest.id, sources)
    out_path = _output_path(plugin.manifest.id, args.out)
    sys.exit(_write_or_check(
        plugin_id=plugin.manifest.id,
        rendered=rendered,
        out_path=out_path,
        check=args.check,
        stdout=args.stdout,
    ))


def _parse_schemas_arg(raw: list[str] | str) -> list[str]:
    """Parse ``--schemas`` argv into a flat list of refs.

    argparse's ``nargs='+'`` returns a list when the flag is repeated
    (e.g. ``--schemas a:A --schemas b:B``); we also accept a single
    comma-separated value (``--schemas a:A,b:B``) which is the form
    pre-commit hooks tend to invoke. Both forms collapse to a flat
    list of refs.
    """
    if isinstance(raw, str):
        items = [raw]
    else:
        items = list(raw)
    refs: list[str] = []
    for item in items:
        for ref in item.split(","):
            ref = ref.strip()
            if ref:
                refs.append(ref)
    return refs


def attach_typegen_subparser(set_sub) -> None:
    """Wire ``graph set typegen`` onto the ``set`` subparsers object."""
    p = set_sub.add_parser(
        "typegen",
        help=(
            "Generate TypeScript .d.ts files from a plugin's schemas. "
            "Use --check as a pre-commit drift gate."
        ),
    )
    target = p.add_mutually_exclusive_group()
    target.add_argument(
        "--plugin",
        metavar="ID",
        help="Plugin id (matches plugin.yaml `id:` field)",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="Generate for every discovered plugin",
    )
    target.add_argument(
        "--schemas",
        metavar="REF",
        nargs="+",
        help=(
            "Generate from an arbitrary list of `module:Class` refs "
            "(comma- or space-separated). Use with --name to set the "
            "output filename. Used for capability-layer schemas that "
            "don't belong to a dashboard plugin."
        ),
    )
    p.add_argument(
        "--name",
        metavar="LABEL",
        help=(
            "Output filename stem (and header label) for --schemas mode "
            "(default output: tools/dashboard/static/js/types/<LABEL>.d.ts)"
        ),
    )
    p.add_argument(
        "--out",
        metavar="PATH",
        help=(
            "Custom output path (default: "
            "tools/dashboard/static/js/types/<plugin-id>.d.ts)"
        ),
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="Write generated content to stdout instead of a file",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "Exit non-zero if the on-disk file diverges from the freshly "
            "generated content (drift gate for pre-commit / CI)"
        ),
    )
    p.set_defaults(func=cmd_set_typegen)
