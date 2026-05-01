"""Tests for the enriched ``export_json_schema()`` payload (bead 1D).

The meta-Setting payload that codegen consumers read carries:

* ``set_id`` / ``schema_revision`` (root-only)
* ``type`` / ``properties`` / ``required`` — JSON-schema-ish core
* ``access_pattern`` / ``key_strategy`` (root-only; ``None`` for
  undecorated schemas)
* ``variants`` — recursive map of discriminator slug to that variant's
  payload, where each variant's payload follows the same shape but
  omits the root-only fields

The recursion preserves the tree shape that ``__init_subclass__``
records: nested-namespace consumers walk the tree to assemble dotted
paths like ``source_control.review.read``.
"""

from __future__ import annotations

import pytest

from tools.graph.schemas import (
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
    singleton,
)
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


@pytest.fixture(autouse=True)
def _isolate_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


# ── access_pattern + key_strategy ────────────────────────────


def test_undecorated_schema_emits_none_access_pattern():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        name: str = field(required=True)

    js = V1.export_json_schema()
    assert js["access_pattern"] is None
    assert js["key_strategy"] is None


def test_append_only_log_emits_pattern_and_uuid_strategy():
    @append_only_log
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    js = V1.export_json_schema()
    assert js["access_pattern"] == "append_only_log"
    assert js["key_strategy"] == "uuid_v4"


def test_singleton_emits_fixed_key_strategy():
    @singleton(key="canonical")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    js = V1.export_json_schema()
    assert js["access_pattern"] == "singleton"
    assert js["key_strategy"] == "fixed:canonical"


def test_keyed_per_entity_emits_natural_strategy():
    @keyed_per_entity
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    js = V1.export_json_schema()
    assert js["access_pattern"] == "keyed_per_entity"
    assert js["key_strategy"] == "natural"


# ── variants — recursive tree ────────────────────────────────


def test_no_variants_emits_empty_variants_dict():
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    js = V1.export_json_schema()
    assert js["variants"] == {}


def test_variants_emits_keyed_by_slug():
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile reference")

    class ThumbYes(Decision):
        pass

    class ThumbNo(Decision):
        pass

    class Choice(Decision):
        choice: str = field(required=True, description="Picked text")

    js = Decision.export_json_schema()
    assert set(js["variants"]) == {"thumb_yes", "thumb_no", "choice"}


def test_variant_payload_has_recursive_shape():
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile reference")

    class Choice(Decision):
        choice: str = field(required=True, description="Picked text")

    js = Decision.export_json_schema()
    choice = js["variants"]["choice"]
    # Variant payload has the same recursion shape, omits root-only
    # fields like set_id / schema_revision / access_pattern.
    assert choice["type"] == "object"
    assert "properties" in choice
    assert "required" in choice
    assert "variants" in choice
    assert "set_id" not in choice
    assert "schema_revision" not in choice
    assert "access_pattern" not in choice
    assert "key_strategy" not in choice


def test_variant_payload_includes_inherited_base_fields():
    """Variant payload's ``properties`` carries the merged base +
    variant fields (composition with auto-vumin's MRO walk).
    """
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Base field")

    class Choice(Decision):
        choice: str = field(required=True, description="Variant field")

    js = Decision.export_json_schema()
    choice_props = js["variants"]["choice"]["properties"]
    # Base + variant fields both present.
    assert "tile_id" in choice_props
    assert "choice" in choice_props
    assert choice_props["tile_id"]["description"] == "Base field"
    assert choice_props["choice"]["description"] == "Variant field"
    # Required reflects both.
    assert set(js["variants"]["choice"]["required"]) == {"tile_id", "choice"}


def test_nested_namespace_recursion_preserves_tree_shape():
    """The canonical ``SourceControl`` / ``Review`` / ``Gates`` pattern
    from the design signpost — nested namespaces produce a recursive
    payload tree consumers walk to render dotted paths.
    """
    class SourceControl(SettingSchema):
        set_id = "autonomy.capability-contract"
        schema_revision = 1

    class Review(SourceControl):
        pass

    class ReviewRead(Review):
        branch: str = field(required=True, description="Refspec")

    class ReviewRefresh(Review):
        pass

    class Gates(SourceControl):
        pass

    class GatesSnapshot(Gates):
        pass

    js = SourceControl.export_json_schema()
    # Top-level variants.
    assert set(js["variants"]) == {"review", "gates"}
    # Review's variants nested under it.
    review_variants = js["variants"]["review"]["variants"]
    assert set(review_variants) == {"review_read", "review_refresh"}
    # ReviewRead's branch field surfaces with description.
    assert review_variants["review_read"]["properties"]["branch"]["description"] == "Refspec"
    assert review_variants["review_read"]["required"] == ["branch"]
    # Gates's variants nested under it (separate from Review's).
    gates_variants = js["variants"]["gates"]["variants"]
    assert set(gates_variants) == {"gates_snapshot"}


def test_variant_with_no_extra_fields_emits_only_inherited():
    """A variant whose body is just a docstring should still produce a
    payload with the inherited base fields.
    """
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Base")

    class ThumbYes(Decision):
        """Operator approves the tile's ask."""

    js = Decision.export_json_schema()
    thumb_props = js["variants"]["thumb_yes"]["properties"]
    assert "tile_id" in thumb_props
    assert js["variants"]["thumb_yes"]["required"] == ["tile_id"]
    # No own-variant fields.
    assert set(thumb_props) == {"tile_id"}


def test_root_payload_includes_all_top_level_keys_for_decorated_variant_schema():
    """End-to-end: a decorated variant-bearing schema exports the
    full top-level shape codegen relies on.
    """
    @append_only_log(key="uuid_v4")
    class Decision(SettingSchema):
        set_id = "dashboard.coordinator-decision"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile")

    class ThumbYes(Decision):
        pass

    class Choice(Decision):
        choice: str = field(required=True, description="Picked text")

    js = Decision.export_json_schema()
    # Root-only fields.
    assert js["set_id"] == "dashboard.coordinator-decision"
    assert js["schema_revision"] == 1
    assert js["access_pattern"] == "append_only_log"
    assert js["key_strategy"] == "uuid_v4"
    # Core fields.
    assert js["type"] == "object"
    assert "tile_id" in js["properties"]
    assert js["required"] == ["tile_id"]
    # Variants tree.
    assert set(js["variants"]) == {"thumb_yes", "choice"}
    assert js["variants"]["choice"]["properties"]["choice"]["description"] == "Picked text"


def test_idempotent_repeated_export():
    """Calling export_json_schema twice produces equivalent payloads —
    no mutation between calls.
    """
    @append_only_log
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile")

    class ThumbYes(Decision):
        pass

    first = Decision.export_json_schema()
    second = Decision.export_json_schema()
    assert first == second
