"""``@signer(tier)`` — which key signs a set's organization rows (auto-kxkax).

Design of record graph://21a0da9e-1c2, drivers D7/D8. The declaration is
revision-aware and defaults to ``delegate``; it says which key signs where
signing applies, never whether a set is signed (that follows the store) and
never where the set lives (``_assert_home`` is untouched).
"""

from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SchemaValidationError,
    SettingSchema,
    declared_signer,
    home,
    register_schema,
    signer,
)


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


SET_ID = "autonomy.test.signer"


def test_each_declared_tier_resolves_at_its_own_revision():
    @signer("persona")
    class AttendedV1(SettingSchema):
        pass

    register_schema(SET_ID, 1, AttendedV1)
    assert declared_signer(SET_ID, 1) == "persona"

    @signer("delegate")
    class UnattendedV1(SettingSchema):
        pass

    register_schema(SET_ID + ".other", 1, UnattendedV1)
    assert declared_signer(SET_ID + ".other", 1) == "delegate"


def test_silence_resolves_to_delegate_and_is_not_an_error():
    class Quiet(SettingSchema):
        pass

    register_schema(SET_ID, 1, Quiet)
    assert declared_signer(SET_ID, 1) == "delegate"
    # An unregistered revision — and an unregistered set — also resolve to
    # the common case; what a boundary does with a schemaless set is the
    # boundary's policy, not this resolver's.
    assert declared_signer(SET_ID, 7) == "delegate"
    assert declared_signer("autonomy.never.registered", 1) == "delegate"


def test_two_revisions_may_declare_different_tiers_and_both_resolve():
    """The deliberate contrast with declared_vault_tier, which refuses
    disagreement: attendance is decided per contract generation, and a row
    is governed by ITS OWN stored revision's declaration."""

    @signer("persona")
    class V1(SettingSchema):
        pass

    @signer("delegate")
    class V2(SettingSchema):
        pass

    register_schema(SET_ID, 1, V1)
    register_schema(SET_ID, 2, V2)
    assert declared_signer(SET_ID, 1) == "persona"
    assert declared_signer(SET_ID, 2) == "delegate"


def test_a_defaulted_schema_is_distinguishable_from_a_reviewed_delegate():
    """declared_signer collapses silence into delegate BY DESIGN; the
    collapse must stay auditable. A schema that forgot @signer('persona')
    signs unattended — introspection and the schema export are where that
    shows as 'defaulted' rather than 'reviewed'."""
    from tools.graph.schemas.registry import signer_declaration

    @signer("delegate")
    class Reviewed(SettingSchema):
        pass

    class Forgot(SettingSchema):
        pass

    register_schema(SET_ID + ".reviewed", 1, Reviewed)
    register_schema(SET_ID + ".forgot", 1, Forgot)

    # The resolved tier is identical — that is the collapse.
    assert declared_signer(SET_ID + ".reviewed", 1) == "delegate"
    assert declared_signer(SET_ID + ".forgot", 1) == "delegate"

    # The declaration record is not.
    assert signer_declaration(SET_ID + ".reviewed", 1) == {
        "tier": "delegate", "explicit": True,
    }
    assert signer_declaration(SET_ID + ".forgot", 1) == {
        "tier": "delegate", "explicit": False,
    }
    assert signer_declaration("autonomy.never.registered", 1) == {
        "tier": "delegate", "explicit": False,
    }

    # And the export carries both, so an audit can list every defaulted
    # schema without importing each class.
    assert Reviewed.export_json_schema()["signer"] == {
        "tier": "delegate", "explicit": True,
    }
    assert Forgot.export_json_schema()["signer"] == {
        "tier": "delegate", "explicit": False,
    }


def test_a_persona_declaration_exports_as_explicit():
    @signer("persona")
    class Attended(SettingSchema):
        pass

    assert Attended.export_json_schema()["signer"] == {
        "tier": "persona", "explicit": True,
    }


def test_an_unknown_tier_is_refused_at_declaration_time():
    with pytest.raises(SchemaValidationError) as caught:
        signer("root")
    assert "root" in str(caught.value)
    assert "persona" in str(caught.value) and "delegate" in str(caught.value)


def test_conflicting_double_declaration_is_refused():
    with pytest.raises(SchemaValidationError):
        @signer("delegate")
        @signer("persona")
        class TwoMinds(SettingSchema):
            pass


def test_signer_composes_with_any_home_declaration():
    """@signer says which key signs where signing applies — it neither
    implies nor constrains where the set lives."""

    @signer("persona")
    @home("personal")
    class PersonalHomed(SettingSchema):
        pass

    @signer("delegate")
    @home("machine")
    class MachineHomed(SettingSchema):
        pass

    register_schema(SET_ID + ".personal", 1, PersonalHomed)
    register_schema(SET_ID + ".machine", 1, MachineHomed)
    assert declared_signer(SET_ID + ".personal", 1) == "persona"
    assert declared_signer(SET_ID + ".machine", 1) == "delegate"


def test_assert_home_still_permits_the_operators_own_db_for_an_org_set():
    """_assert_home is unchanged: an organization-homed set may hold the
    operator's own (unsigned) row in personal.db — that is D4's sovereignty,
    and @signer must not have narrowed it."""

    @signer("persona")
    @home("organization")
    class OrgHomed(SettingSchema):
        pass

    register_schema(SET_ID + ".org", 1, OrgHomed)
    settings_ops._assert_home(SET_ID + ".org", None)  # personal.db: permitted
    settings_ops._assert_home(SET_ID + ".org", "someorg")  # its own org: permitted
