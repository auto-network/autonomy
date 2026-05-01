"""Tests for ``graph set typegen`` — schema-derived TypeScript codegen.

The generation core is a pure function over an exported-schema payload
shape, so most tests exercise it directly without spinning up plugin
discovery. CLI-level tests cover ``--plugin``, ``--all``, ``--check``,
``--stdout``, and the failure paths.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tools.graph import typegen_cmd
from tools.graph.typegen_cmd import (
    SchemaSource,
    TypegenError,
    _pascal,
    _resolve_schema_entry,
    _ts_type,
    cmd_set_typegen,
    render_dts,
)


# ── Type rendering ──────────────────────────────────────────


def test_ts_type_string():
    assert _ts_type({"type": "string"}) == "string"


def test_ts_type_integer_and_number_both_map_to_number():
    assert _ts_type({"type": "integer"}) == "number"
    assert _ts_type({"type": "number"}) == "number"


def test_ts_type_boolean():
    assert _ts_type({"type": "boolean"}) == "boolean"


def test_ts_type_object_defaults_to_record():
    assert _ts_type({"type": "object"}) == "Record<string, unknown>"


def test_ts_type_enum_emits_literal_union():
    out = _ts_type({"type": "string", "enum": ["claude", "codex"]})
    assert out == "'claude' | 'codex'"


def test_ts_type_array_of_string_via_bare_element_name():
    assert _ts_type({"type": "array", "element": "string"}) == "string[]"


def test_ts_type_array_of_string_via_property_descriptor_element():
    # Substrate's `element={"type": "string"}` shape — used by capability
    # impl's required_env / required_secret_files / tool_paths.
    assert _ts_type({"type": "array", "element": {"type": "string"}}) == "string[]"


def test_ts_type_array_of_object_with_required_and_optional_fields():
    # Mirrors capability_impl#1.implements: each element has required
    # contract+version sub-fields.
    out = _ts_type({
        "type": "array",
        "element": {
            "contract": {"type": "string", "required": True},
            "version": {"type": "integer", "required": True},
        },
    })
    assert out == "{ contract: string; version: number }[]"


def test_ts_type_array_of_object_optional_subfield_uses_question_mark():
    out = _ts_type({
        "type": "array",
        "element": {
            "url": {"type": "string", "required": True},
            "writable": {"type": "boolean"},  # no required → optional
        },
    })
    assert out == "{ url: string; writable?: boolean }[]"


def test_ts_type_array_of_object_with_legacy_type_name_values():
    # Legacy shape: `element={"url": "string"}` (bare type-name strings,
    # not property dicts) — treat each entry as required.
    out = _ts_type({
        "type": "array",
        "element": {"url": "string", "mount": "string"},
    })
    assert out == "{ url: string; mount: string }[]"


def test_ts_type_unknown_falls_back_to_unknown():
    assert _ts_type({"type": "weird-type"}) == "unknown"


# ── Pascal helper ────────────────────────────────────────────


@pytest.mark.parametrize("slug,expected", [
    ("thumb_yes", "ThumbYes"),
    ("refresh_request", "RefreshRequest"),
    ("simple", "Simple"),
    ("a_b_c", "ABC"),
    ("review_read", "ReviewRead"),
])
def test_pascal_helper(slug, expected):
    assert _pascal(slug) == expected


# ── Interface rendering ──────────────────────────────────────


def _flat_payload() -> dict:
    """Mirror what export_json_schema returns for a flat schema.

    Modeled on org_capability_install#1's actual export so the test
    catches a regression if the substrate's payload shape drifts.
    """
    return {
        "type": "object",
        "properties": {
            "contract": {
                "type": "string",
                "description": "Contract identifier",
            },
            "contract_version": {
                "type": "integer",
                "description": "Pinned contract version (>= 1)",
            },
            "env_bindings": {
                "type": "object",
                "description": "Env var bindings: env name -> source identifier",
            },
            "notes": {"type": "string", "description": "Free-form notes"},
        },
        "required": ["contract", "contract_version"],
        "variants": {},
        "set_id": "autonomy.org.capability.install",
        "schema_revision": 1,
        "access_pattern": None,
        "key_strategy": None,
    }


def test_render_dts_for_a_flat_schema_emits_one_interface():
    src = SchemaSource(
        ref="tools.graph.schemas.org_capability_install:OrgCapabilityInstallV1",
        cls_name="OrgCapabilityInstallV1",
        payload=_flat_payload(),
    )
    out = render_dts("autonomy-test", [src])
    # Header: provenance line, source listing.
    assert "AUTO-GENERATED" in out
    assert "graph set typegen --plugin autonomy-test" in out
    assert "tools.graph.schemas.org_capability_install:OrgCapabilityInstallV1" in out
    # Interface body.
    assert "export interface OrgCapabilityInstallV1 {" in out
    # Required fields use `:`; optional use `?:`.
    assert "  contract: string;" in out
    assert "  contract_version: number;" in out
    # Optional dict-typed field maps to Record.
    assert "  env_bindings?: Record<string, unknown>;" in out
    assert "  notes?: string;" in out
    # JSDoc comment per field.
    assert "/** Contract identifier */" in out
    # Header carries the set_id#rev for grep-back.
    assert "* autonomy.org.capability.install#1" in out
    # No discriminated union for a flat schema.
    assert "export type" not in out


def test_render_dts_with_no_schemas_still_emits_header():
    out = render_dts("empty-plugin", [])
    assert "AUTO-GENERATED" in out
    assert "(no schemas declared)" in out
    assert "export interface" not in out


# ── Variant union rendering ──────────────────────────────────


def _variant_payload() -> dict:
    """Mirror an exported variant-bearing schema.

    Single base field + four variants (two with extra fields, two
    without). Modeled on the canonical Decision pattern from
    graph://865295b3-5cc.
    """
    return {
        "type": "object",
        "properties": {
            "tile_id": {
                "type": "string",
                "description": "Which tile this decision belongs to.",
            },
        },
        "required": ["tile_id"],
        "variants": {
            "thumb_yes": {
                "type": "object",
                "properties": {
                    "tile_id": {"type": "string", "description": "Which tile this decision belongs to."},
                },
                "required": ["tile_id"],
                "variants": {},
            },
            "thumb_no": {
                "type": "object",
                "properties": {
                    "tile_id": {"type": "string", "description": "Which tile this decision belongs to."},
                },
                "required": ["tile_id"],
                "variants": {},
            },
            "choice": {
                "type": "object",
                "properties": {
                    "tile_id": {"type": "string", "description": "Which tile this decision belongs to."},
                    "choice": {"type": "string", "description": "Picked text"},
                },
                "required": ["tile_id", "choice"],
                "variants": {},
            },
            "custom": {
                "type": "object",
                "properties": {
                    "tile_id": {"type": "string", "description": "Which tile this decision belongs to."},
                    "choice": {"type": "string", "description": "Free-text body"},
                },
                "required": ["tile_id", "choice"],
                "variants": {},
            },
        },
        "set_id": "dashboard.coordinator-decision",
        "schema_revision": 1,
        "access_pattern": "append_only_log",
        "key_strategy": "uuid_v4",
    }


def test_render_dts_for_variant_schema_emits_base_plus_per_variant_interfaces_plus_union():
    src = SchemaSource(
        ref="example.module:Decision",
        cls_name="Decision",
        payload=_variant_payload(),
    )
    out = render_dts("decision-plugin", [src])
    # Base interface carries the parent's fields, no `kind`.
    assert "export interface DecisionBase {" in out
    assert "  tile_id: string;" in out
    # Per-variant interfaces extend the base and carry `kind: 'slug'`.
    assert "export interface DecisionThumbYes extends DecisionBase {" in out
    assert "  kind: 'thumb_yes';" in out
    assert "export interface DecisionChoice extends DecisionBase {" in out
    assert "  kind: 'choice';" in out
    assert "  choice: string;" in out
    # Discriminated union at the bottom names every variant.
    assert "export type Decision =" in out
    assert "| DecisionThumbYes" in out
    assert "| DecisionThumbNo" in out
    assert "| DecisionChoice" in out
    assert "| DecisionCustom" in out
    # Header surfaces access pattern + key strategy for the operator.
    assert "Access pattern: append_only_log (key strategy: uuid_v4)" in out


# ── Nested variant tree (recursion) ──────────────────────────


def _nested_variant_payload() -> dict:
    """Mirror an exported nested-variant schema.

    Models the canonical capability-layer pattern from
    graph://865295b3-5cc and the substrate's
    ``test_capability_layer_nested_namespace_shape``:

    * ``SourceControl`` (root)
      * ``branch_status`` (leaf, no further variants)
      * ``commit_stack`` (leaf, no further variants)
      * ``review`` (namespace)
        * ``review_read`` (leaf, with own ``branch`` field)
        * ``review_refresh`` (leaf, no extra fields)
      * ``gates`` (namespace)
        * ``gates_snapshot`` (leaf)
        * ``gates_watch_set`` (leaf)
    """
    base_props = {
        "tile_id": {"type": "string", "description": "Workspace tile reference"},
    }
    base_required = ["tile_id"]
    leaf = {
        "type": "object",
        "properties": dict(base_props),
        "required": list(base_required),
        "variants": {},
    }
    review_read = {
        "type": "object",
        "properties": {
            **base_props,
            "branch": {"type": "string", "description": "Refspec to look up."},
        },
        "required": ["tile_id", "branch"],
        "variants": {},
    }
    review = {
        "type": "object",
        "properties": dict(base_props),
        "required": list(base_required),
        "variants": {
            "review_read": review_read,
            "review_refresh": dict(leaf),
        },
    }
    gates = {
        "type": "object",
        "properties": dict(base_props),
        "required": list(base_required),
        "variants": {
            "gates_snapshot": dict(leaf),
            "gates_watch_set": dict(leaf),
        },
    }
    return {
        "type": "object",
        "properties": dict(base_props),
        "required": list(base_required),
        "variants": {
            "branch_status": dict(leaf),
            "commit_stack": dict(leaf),
            "review": review,
            "gates": gates,
        },
        "set_id": "autonomy.capability.contract.source_control",
        "schema_revision": 1,
        "access_pattern": None,
        "key_strategy": None,
    }


def test_render_dts_recurses_into_nested_variants():
    """The capability-layer SourceControl/Review/Gates pattern: every
    variant at every depth must produce its own interface, AND every
    variant must appear in the top-level discriminated union — not
    only the direct children of the root.
    """
    src = SchemaSource(
        ref="example.module:SourceControl",
        cls_name="SourceControl",
        payload=_nested_variant_payload(),
    )
    out = render_dts("source-control-plugin", [src])

    # Base + every direct child interface.
    assert "export interface SourceControlBase {" in out
    assert "export interface SourceControlBranchStatus extends SourceControlBase {" in out
    assert "export interface SourceControlCommitStack extends SourceControlBase {" in out
    assert "export interface SourceControlReview extends SourceControlBase {" in out
    assert "export interface SourceControlGates extends SourceControlBase {" in out

    # Every nested variant must also appear, NOT chained through the
    # parent variant — chaining would clash on the `kind` literal type.
    assert "export interface SourceControlReviewReviewRead extends SourceControlBase {" in out
    assert "export interface SourceControlReviewReviewRefresh extends SourceControlBase {" in out
    assert "export interface SourceControlGatesGatesSnapshot extends SourceControlBase {" in out
    assert "export interface SourceControlGatesGatesWatchSet extends SourceControlBase {" in out

    # Every variant carries its own discriminator slug — including the
    # leaves nested two levels deep.
    assert "  kind: 'branch_status';" in out
    assert "  kind: 'review';" in out
    assert "  kind: 'review_read';" in out
    assert "  kind: 'gates_watch_set';" in out

    # Nested-variant fields propagate (the substrate already merges
    # inherited fields into each variant's `properties`).
    assert "  branch: string;" in out

    # Discriminated union enumerates every variant in the tree.
    union_idx = out.index("export type SourceControl =")
    union_block = out[union_idx:]
    for member in [
        "SourceControlBranchStatus",
        "SourceControlCommitStack",
        "SourceControlReview",
        "SourceControlReviewReviewRead",
        "SourceControlReviewReviewRefresh",
        "SourceControlGates",
        "SourceControlGatesGatesSnapshot",
        "SourceControlGatesGatesWatchSet",
    ]:
        assert f"| {member}" in union_block, (
            f"discriminated union missing {member}; got:\n{union_block}"
        )


def test_render_dts_recurses_through_three_levels():
    """Pin the recursion depth: a third-level nested variant must
    still appear in the union and its own interface. The substrate
    doesn't cap nesting, so codegen mustn't either.
    """
    grandchild = {
        "type": "object",
        "properties": {},
        "required": [],
        "variants": {},
    }
    child = {
        "type": "object",
        "properties": {},
        "required": [],
        "variants": {"grandchild_leaf": grandchild},
    }
    payload = {
        "type": "object",
        "properties": {},
        "required": [],
        "variants": {"child_branch": child},
        "set_id": "x.y",
        "schema_revision": 1,
        "access_pattern": None,
        "key_strategy": None,
    }
    src = SchemaSource(ref="x:Root", cls_name="Root", payload=payload)
    out = render_dts("plugin", [src])

    assert "export interface RootChildBranch extends RootBase {" in out
    assert "export interface RootChildBranchGrandchildLeaf extends RootBase {" in out
    assert "  kind: 'child_branch';" in out
    assert "  kind: 'grandchild_leaf';" in out
    assert "| RootChildBranch" in out
    assert "| RootChildBranchGrandchildLeaf" in out


def test_render_dts_omits_access_pattern_line_for_undecorated_schema():
    src = SchemaSource(
        ref="x:Y",
        cls_name="Y",
        payload=_flat_payload(),  # access_pattern: None
    )
    out = render_dts("plain-plugin", [src])
    assert "Access pattern:" not in out


# ── Schema entry resolution ──────────────────────────────────


def test_resolve_schema_entry_round_trips_through_export_json_schema():
    """End-to-end: a real schema reference produces a SchemaSource
    whose payload matches the live export_json_schema() output.
    """
    ref = (
        "tools.graph.schemas.org_capability_install:OrgCapabilityInstallV1"
    )
    source = _resolve_schema_entry(ref)
    assert source.ref == ref
    assert source.cls_name == "OrgCapabilityInstallV1"
    assert source.payload["set_id"] == "autonomy.org.capability.install"
    assert source.payload["schema_revision"] == 1
    assert "contract" in source.payload["properties"]


def test_resolve_schema_entry_rejects_missing_colon():
    with pytest.raises(TypegenError, match="must be 'module:ClassName'"):
        _resolve_schema_entry("no_colon_anywhere")


def test_resolve_schema_entry_rejects_unknown_module():
    with pytest.raises(TypegenError, match="cannot import module"):
        _resolve_schema_entry("totally.fake.module:Anything")


def test_resolve_schema_entry_rejects_unknown_attribute():
    with pytest.raises(TypegenError, match="has no attribute"):
        _resolve_schema_entry(
            "tools.graph.schemas.org_capability_install:NoSuchClass"
        )


def test_resolve_schema_entry_rejects_non_schema_class():
    # ``json.JSONDecoder`` exists but isn't a SettingSchema subclass.
    with pytest.raises(TypegenError, match="not a SettingSchema subclass"):
        _resolve_schema_entry("json:JSONDecoder")


# ── CLI ────────────────────────────────────────────────────


def _make_args(**overrides):
    """Build an argparse Namespace matching attach_typegen_subparser's shape."""
    defaults = dict(
        plugin=None, all=False, schemas=None, name=None,
        out=None, stdout=False, check=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_typegen_writes_output_to_disk(tmp_path, monkeypatch):
    """Happy path: --plugin <id> writes the .d.ts to disk."""
    out_path = tmp_path / "fake-plugin.d.ts"

    monkeypatch.setattr(
        typegen_cmd,
        "_discover_plugin",
        lambda pid: _stub_plugin(pid),
    )
    monkeypatch.setattr(
        typegen_cmd,
        "_resolve_plugin_sources",
        lambda plugin: [SchemaSource("x:Y", "Y", _flat_payload())],
    )

    args = _make_args(plugin="fake-plugin", out=str(out_path))
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 0
    body = out_path.read_text()
    assert "AUTO-GENERATED" in body
    assert "export interface Y {" in body


def test_cmd_typegen_check_passes_when_disk_matches(tmp_path, monkeypatch, capsys):
    out_path = tmp_path / "fake.d.ts"
    monkeypatch.setattr(typegen_cmd, "_discover_plugin", lambda pid: _stub_plugin(pid))
    monkeypatch.setattr(
        typegen_cmd,
        "_resolve_plugin_sources",
        lambda plugin: [SchemaSource("x:Y", "Y", _flat_payload())],
    )

    # Write the canonical content first.
    args = _make_args(plugin="fake-plugin", out=str(out_path))
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 0
    # Now run with --check; should pass.
    args = _make_args(plugin="fake-plugin", out=str(out_path), check=True)
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 0


def test_cmd_typegen_check_fails_when_disk_diverges(tmp_path, monkeypatch, capsys):
    out_path = tmp_path / "fake.d.ts"
    out_path.write_text("// hand-edited junk that doesn't match\n")
    monkeypatch.setattr(typegen_cmd, "_discover_plugin", lambda pid: _stub_plugin(pid))
    monkeypatch.setattr(
        typegen_cmd,
        "_resolve_plugin_sources",
        lambda plugin: [SchemaSource("x:Y", "Y", _flat_payload())],
    )
    args = _make_args(plugin="fake-plugin", out=str(out_path), check=True)
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "drift" in err.lower()
    assert str(out_path) in err
    # On-disk content untouched.
    assert "hand-edited junk" in out_path.read_text()


def test_cmd_typegen_check_fails_when_disk_missing(tmp_path, monkeypatch, capsys):
    out_path = tmp_path / "missing.d.ts"
    monkeypatch.setattr(typegen_cmd, "_discover_plugin", lambda pid: _stub_plugin(pid))
    monkeypatch.setattr(
        typegen_cmd,
        "_resolve_plugin_sources",
        lambda plugin: [SchemaSource("x:Y", "Y", _flat_payload())],
    )
    args = _make_args(plugin="fake-plugin", out=str(out_path), check=True)
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_cmd_typegen_stdout_prints_and_skips_disk(tmp_path, monkeypatch, capsys):
    out_path = tmp_path / "should-not-exist.d.ts"
    monkeypatch.setattr(typegen_cmd, "_discover_plugin", lambda pid: _stub_plugin(pid))
    monkeypatch.setattr(
        typegen_cmd,
        "_resolve_plugin_sources",
        lambda plugin: [SchemaSource("x:Y", "Y", _flat_payload())],
    )
    args = _make_args(plugin="fake-plugin", out=str(out_path), stdout=True)
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 0
    assert "AUTO-GENERATED" in capsys.readouterr().out
    assert not out_path.exists()


def test_cmd_typegen_missing_plugin_id_is_a_usage_error(monkeypatch, capsys):
    args = _make_args()  # no --plugin, no --all
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 2
    assert "--plugin" in capsys.readouterr().err


def test_cmd_typegen_unknown_plugin_exits_one(monkeypatch, capsys):
    def boom(pid):
        raise TypegenError(f"plugin {pid!r} not found under tools/dashboard/plugins")

    monkeypatch.setattr(typegen_cmd, "_discover_plugin", boom)
    args = _make_args(plugin="does-not-exist")
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err


def test_cmd_typegen_all_iterates_every_plugin(tmp_path, monkeypatch):
    """--all generates one file per discovered plugin."""
    p1 = _stub_plugin("alpha", out_dir=tmp_path)
    p2 = _stub_plugin("beta", out_dir=tmp_path)
    monkeypatch.setattr(typegen_cmd, "_discover_all_plugins", lambda: [p1, p2])
    monkeypatch.setattr(
        typegen_cmd,
        "_resolve_plugin_sources",
        lambda plugin: [SchemaSource("x:Y", "Y", _flat_payload())],
    )
    monkeypatch.setattr(
        typegen_cmd,
        "DEFAULT_OUTPUT_ROOT",
        tmp_path,
    )
    args = _make_args(all=True)
    with pytest.raises(SystemExit) as ei:
        cmd_set_typegen(args)
    assert ei.value.code == 0
    assert (tmp_path / "alpha.d.ts").exists()
    assert (tmp_path / "beta.d.ts").exists()


# ── Live drift gate ──────────────────────────────────────────


def test_committed_dts_files_match_live_schemas():
    """Every plugin that declares ``entrypoints.schemas`` must have a
    matching ``.d.ts`` checked into ``tools/dashboard/static/js/types/``,
    and the on-disk content must equal what ``render_dts`` produces from
    the live ``export_json_schema`` payloads.

    This is the codegen drift gate as a regular pytest test — operators
    can run ``graph set typegen --plugin <id> --check`` manually, but
    fail-on-drift in the test suite catches the divergence the moment a
    schema edit lands without regenerating its types.
    """
    from tools.dashboard.plugin_api.loader import discover

    failures: list[str] = []
    for plugin in discover():
        refs = plugin.manifest.entrypoints.schemas or []
        if not refs:
            continue
        sources = [_resolve_schema_entry(ref) for ref in refs]
        rendered = render_dts(plugin.manifest.id, sources)
        on_disk = typegen_cmd.DEFAULT_OUTPUT_ROOT / f"{plugin.manifest.id}.d.ts"
        if not on_disk.exists():
            failures.append(
                f"{plugin.manifest.id}: missing {on_disk} — run "
                f"`graph set typegen --plugin {plugin.manifest.id}`"
            )
            continue
        if on_disk.read_text() != rendered:
            failures.append(
                f"{plugin.manifest.id}: {on_disk} is out of date — run "
                f"`graph set typegen --plugin {plugin.manifest.id}` to refresh"
            )
    if failures:
        pytest.fail("typegen drift:\n  " + "\n  ".join(failures))


# ── Test stubs ──────────────────────────────────────────────


class _StubManifest:
    def __init__(self, pid: str):
        self.id = pid


class _StubPlugin:
    def __init__(self, pid: str, out_dir: Path | None = None):
        self.manifest = _StubManifest(pid)
        self.plugin_dir = out_dir or Path("/nonexistent")


def _stub_plugin(pid: str, out_dir: Path | None = None) -> _StubPlugin:
    return _StubPlugin(pid, out_dir=out_dir)
