"""Tests for variant-subclass enumeration on SettingSchema.

A ``SettingSchema`` subclass whose immediate parent is itself a
``SettingSchema`` descendant (not ``SettingSchema`` directly) is treated
as a variant of that parent. ``__init_subclass__`` stamps
``_variant_slug = snake_case(cls.__name__)`` on the variant and adds it
to the parent's ``_variants`` dict.

Direct ``SettingSchema`` subclasses are fresh bases (``_variant_slug is
None``); their ``_variants`` dict is empty until variants subclass them.

Multi-level hierarchies preserve tree shape — each level registers only
its direct children. Bead 1D consumes this tree to render the meta-
Setting payload.
"""

from __future__ import annotations

import pytest

from tools.graph.schemas import (
    SettingSchema,
    append_only_log,
    field,
)
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS, snake_case


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


# ── Slug helper ──────────────────────────────────────────────


@pytest.mark.parametrize("name,slug", [
    ("ThumbYes", "thumb_yes"),
    ("ThumbNo", "thumb_no"),
    ("Choice", "choice"),
    ("Custom", "custom"),
    ("RefreshRequest", "refresh_request"),
    ("SitrepRequest", "sitrep_request"),
    ("MyURL", "my_url"),
    ("URLToFoo", "url_to_foo"),
    ("A", "a"),
    ("ABC", "abc"),
    ("already_snake", "already_snake"),
    ("HTTPSProxy", "https_proxy"),
])
def test_snake_case_helper(name, slug):
    assert snake_case(name) == slug


# ── Variant enumeration ──────────────────────────────────────


def test_direct_setting_schema_subclass_is_not_a_variant():
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    assert Base._variant_slug is None
    assert Base._variants == {}


def test_variant_subclass_registers_under_parent():
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    class ThumbYes(Base):
        pass

    assert Base._variants == {"thumb_yes": ThumbYes}
    assert ThumbYes._variant_slug == "thumb_yes"


def test_variant_subclass_initializes_own_empty_variants_registry():
    """Each variant gets its own ``_variants = {}`` so it can host
    further variants without sharing the parent's mutable default.
    """
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    class ThumbYes(Base):
        pass

    assert ThumbYes._variants == {}
    # The two dicts are distinct objects (no aliasing).
    assert ThumbYes._variants is not Base._variants


def test_multiple_siblings_register_under_same_parent():
    class Decision(SettingSchema):
        set_id = "dashboard.coordinator-decision"
        schema_revision = 1

    class ThumbYes(Decision): pass
    class ThumbNo(Decision): pass
    class Choice(Decision): pass
    class Custom(Decision): pass
    class SitrepRequest(Decision): pass
    class RefreshRequest(Decision): pass

    assert set(Decision._variants) == {
        "thumb_yes", "thumb_no", "choice", "custom",
        "sitrep_request", "refresh_request",
    }
    assert Decision._variants["thumb_yes"] is ThumbYes
    assert Decision._variants["refresh_request"] is RefreshRequest


def test_unrelated_schemas_do_not_pollute_each_others_variants():
    class A(SettingSchema):
        set_id = "x.a"
        schema_revision = 1

    class B(SettingSchema):
        set_id = "x.b"
        schema_revision = 1

    class AVariant(A): pass
    class BVariant(B): pass

    assert set(A._variants) == {"a_variant"}
    assert set(B._variants) == {"b_variant"}


# ── Multi-level (nested namespace) ───────────────────────────


def test_nested_variant_registers_under_immediate_parent_only():
    """Multi-level hierarchies preserve tree shape — each level records
    only its direct children. ``ThumbYesPartial`` goes in
    ``ThumbYes._variants``, NOT ``Decision._variants``.
    """
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    class ThumbYes(Decision):
        pass

    class ThumbYesPartial(ThumbYes):
        pass

    # Top level: only ThumbYes is a direct variant.
    assert set(Decision._variants) == {"thumb_yes"}
    # Nested level: ThumbYesPartial belongs to ThumbYes.
    assert set(ThumbYes._variants) == {"thumb_yes_partial"}
    assert ThumbYes._variants["thumb_yes_partial"] is ThumbYesPartial
    assert ThumbYesPartial._variant_slug == "thumb_yes_partial"


def test_capability_layer_nested_namespace_shape():
    """Walks the SourceControl/Review/Gates pattern from the design
    signpost — the canonical nested-namespace use case for variants.
    """
    class SourceControl(SettingSchema):
        set_id = "autonomy.capability-contract"
        schema_revision = 1

    class BranchStatus(SourceControl): pass
    class CommitStack(SourceControl): pass

    class Review(SourceControl):
        pass

    class ReviewRead(Review):
        branch: str = field(description="Refspec to look up.")

    class ReviewRefresh(Review): pass

    class Gates(SourceControl):
        pass

    class GatesSnapshot(Gates): pass
    class GatesWatchSet(Gates): pass

    assert set(SourceControl._variants) == {
        "branch_status", "commit_stack", "review", "gates",
    }
    assert set(Review._variants) == {"review_read", "review_refresh"}
    assert set(Gates._variants) == {"gates_snapshot", "gates_watch_set"}


# ── Composition with field metadata + decorators ─────────────


def test_variant_inherits_base_typed_fields():
    """The auto-vumin inheritance fix must continue to flow base fields
    into variant subclasses — variant enumeration is additive.
    """
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile reference")

    class Choice(Decision):
        choice: str = field(required=True, description="Picked text")

    assert set(Choice._field_metadata) == {"tile_id", "choice"}
    assert Choice._field_metadata["tile_id"]["required"] is True
    assert Choice._field_metadata["choice"]["description"] == "Picked text"


def test_variant_inherits_access_pattern_decorator():
    """Variants inherit ``_access_pattern`` / ``_key_strategy`` via
    standard class-attribute lookup — decorators on the base apply
    to variants without re-decoration.
    """
    @append_only_log(key="uuid_v4")
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    class ThumbYes(Decision):
        pass

    assert ThumbYes._access_pattern == "append_only_log"
    assert ThumbYes._key_strategy == "uuid_v4"


def test_variant_can_redeclare_typed_field_to_override():
    """A variant overriding a base typed field still gets the variant
    discriminator; field-override semantics from auto-vumin continue
    to apply.
    """
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        priority: str = field(default="normal", description="Default priority")

    class Urgent(Base):
        priority: str = field(default="urgent", description="Override priority")

    assert Urgent._variant_slug == "urgent"
    assert Urgent._field_metadata["priority"]["default"] == "urgent"
    assert Urgent._field_metadata["priority"]["description"] == "Override priority"


def test_variant_without_extra_fields_works():
    """A variant whose body is just a docstring (no own fields) still
    enumerates and inherits base metadata correctly.
    """
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile reference")

    class ThumbYes(Decision):
        """Operator approves the tile's ask."""

    assert ThumbYes._variant_slug == "thumb_yes"
    assert set(ThumbYes._field_metadata) == {"tile_id"}


# ── Hygiene ──────────────────────────────────────────────────


def test_variant_slug_uses_shared_helper_consistently():
    """Whatever ``snake_case()`` produces for a name must match what
    ``__init_subclass__`` stores. Pin this so codegen and variant
    enumeration can never drift apart.
    """
    class Base(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    class HTTPSProxyHandler(Base):
        pass

    assert HTTPSProxyHandler._variant_slug == snake_case("HTTPSProxyHandler")


def test_export_json_schema_payload_does_not_leak_variant_metadata():
    """1C stores variant metadata on the class only; surfacing it in
    the meta-Setting payload is bead 1D's job. Pinned regression.
    """
    class Decision(SettingSchema):
        set_id = "x.y"
        schema_revision = 1

    class ThumbYes(Decision):
        pass

    js = Decision.export_json_schema()
    assert "variants" not in js
    # Pinned existing keys still there.
    assert js["set_id"] == "x.y"
    assert js["schema_revision"] == 1
