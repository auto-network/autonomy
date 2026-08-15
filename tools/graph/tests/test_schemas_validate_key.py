"""``validate_key`` — the declared key strategy is checked at the write.

Only the strategies that state a FORM can be checked. ``fixed:X`` means the
key is literally ``X``; ``uuid_v4`` means it is a uuid. The rest name the
ENTITY a key identifies — ``org_slug``, ``workspace_id`` — which says what
the key means rather than what it looks like, and is for a reader.

The checkable half exists for the false-plurality class: a schema declaring
one fixed row while a writer quietly uses a second key is how a singleton
becomes a set nobody designed, and how readers end up scanning for the row
they wanted.
"""
from __future__ import annotations

import pytest

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
    singleton,
    validate_key,
)


@pytest.fixture(scope="module")
def schemas():
    """Module-scoped: the registry is process-global and refuses to
    re-register a set_id, so these are declared once for the file."""
    @singleton(key="default")
    class Only(SettingSchema):
        set_id = "probe.key.only"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @append_only_log(key="uuid_v4")
    class Log(SettingSchema):
        set_id = "probe.key.log"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @keyed_per_entity(key_strategy="org_slug")
    class PerOrg(SettingSchema):
        set_id = "probe.key.perorg"
        schema_revision = 1
        v: str = field(required=True, description="value")

    return Only, Log, PerOrg


def test_a_singleton_accepts_only_its_declared_key(schemas):
    validate_key("probe.key.only", 1, "default")


def test_a_singleton_refuses_a_second_key(schemas):
    """The whole point: this is a singleton quietly becoming a set."""
    with pytest.raises(SchemaValidationError, match="single row keyed"):
        validate_key("probe.key.only", 1, "anchore")


def test_generated_keys_must_look_generated(schemas):
    validate_key("probe.key.log", 1, "000dfb14-7814-4ea7-9247-0574c3dcf968")
    with pytest.raises(SchemaValidationError, match="generated uuids"):
        validate_key("probe.key.log", 1, "hand-written")


def test_an_entity_named_strategy_constrains_no_form(schemas):
    """``org_slug`` says what the key MEANS. It is not a pattern to match.

    Checking it would mean the registry deciding what a valid org slug is,
    which is not its business and would couple schema validation to whether
    an org happens to exist.
    """
    for key in ("autonomy", "anchore", "anything-at-all"):
        validate_key("probe.key.perorg", 1, key)


def test_an_unregistered_schema_is_left_to_payload_validation(schemas):
    """Reporting the unknown schema twice would be noise; validate_payload does it."""
    validate_key("probe.key.nonexistent", 99, "whatever")
