"""Schema + ops tests for ``autonomy.workspace.turn_correction#1``.

Productizes the turn-correction guidance (bead ``auto-edec1.5``). The
Setting is the contract between operators (who decide when and how
aggressively to nudge agents) and the runtime primer (which actually
nudges them). These tests pin both ends:

* schema validation rejects garbage and accepts every documented mode,
* ``ops.add_setting`` / ``ops.upsert_by_key`` round-trip a real payload,
* ``resolve_payload`` layers partial Settings over the canonical
  defaults so the primer never has to repeat ``payload.get(..., x)``.
"""

from __future__ import annotations

import pytest

from tools.graph import ops
from tools.graph.schemas.registry import SchemaValidationError
from tools.graph.schemas.turn_correction import (
    DEFAULT_PAYLOAD,
    SCHEMA_REVISION,
    SET_ID,
    TurnCorrectionSettingsV1,
    VALID_AGGRESSIVENESS,
    resolve_payload,
)


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


# ── Schema constants ─────────────────────────────────────────


def test_set_id_and_revision_are_canonical():
    """The canonical name is ``autonomy.workspace.turn_correction#1``.

    Pinned because the dispatcher / primer renderer / dashboard all
    look up this Setting by exact name; renaming silently is a footgun.
    """
    assert SET_ID == "autonomy.workspace.turn_correction"
    assert SCHEMA_REVISION == 1


def test_valid_aggressiveness_levels_match_cli():
    """The four aggressiveness levels mirror the v1 CLI ``--mode`` choices.

    The agent's primer guidance and the ``graph turn-correction
    suggest --mode`` argument must agree on the same set of strings.
    """
    assert VALID_AGGRESSIVENESS == (
        "off", "conservative", "balanced", "aggressive",
    )


def test_default_payload_is_action_biased_baseline():
    """Defaults: enabled, aggressive, do not persist accepts.

    This is what the renderer falls back to when no Setting exists, so
    the feature is useful even before an operator has authored
    workspace-specific knobs.
    """
    assert DEFAULT_PAYLOAD["enabled"] is True
    assert DEFAULT_PAYLOAD["aggressiveness"] == "aggressive"
    assert DEFAULT_PAYLOAD["persist_accepts_to_graph"] is False


# ── Validation ───────────────────────────────────────────────


def test_validate_accepts_empty_payload():
    """Empty payload is valid — every field is optional."""
    TurnCorrectionSettingsV1.validate({})


@pytest.mark.parametrize("aggressiveness", list(VALID_AGGRESSIVENESS))
def test_validate_accepts_every_aggressiveness_level(aggressiveness):
    TurnCorrectionSettingsV1.validate({"aggressiveness": aggressiveness})


def test_validate_rejects_unknown_aggressiveness():
    with pytest.raises(SchemaValidationError):
        TurnCorrectionSettingsV1.validate({"aggressiveness": "yolo"})


def test_validate_rejects_non_dict_payload():
    with pytest.raises(SchemaValidationError):
        TurnCorrectionSettingsV1.validate("balanced")


def test_validate_rejects_unknown_field():
    with pytest.raises(SchemaValidationError):
        TurnCorrectionSettingsV1.validate({"banana": True})


def test_validate_rejects_non_bool_enabled():
    with pytest.raises(SchemaValidationError):
        TurnCorrectionSettingsV1.validate({"enabled": "true"})


def test_validate_rejects_non_bool_persist_accepts():
    with pytest.raises(SchemaValidationError):
        TurnCorrectionSettingsV1.validate(
            {"persist_accepts_to_graph": 1}
        )


def test_validate_rejects_non_string_instruction_template():
    with pytest.raises(SchemaValidationError):
        TurnCorrectionSettingsV1.validate({"instruction_template": 42})


def test_validate_accepts_full_payload():
    TurnCorrectionSettingsV1.validate({
        "enabled": True,
        "aggressiveness": "aggressive",
        "persist_accepts_to_graph": True,
        "instruction_template": "Custom lead-in text.",
    })


# ── resolve_payload (defaults layering) ──────────────────────


def test_resolve_payload_none_returns_defaults():
    """A workspace with no Setting still gets a fully-populated dict."""
    resolved = resolve_payload(None)
    assert resolved == DEFAULT_PAYLOAD


def test_resolve_payload_empty_returns_defaults():
    resolved = resolve_payload({})
    assert resolved == DEFAULT_PAYLOAD


def test_resolve_payload_partial_layers_over_defaults():
    """Operators write only the fields they care about — the rest fall through."""
    resolved = resolve_payload({"aggressiveness": "off"})
    assert resolved["aggressiveness"] == "off"
    assert resolved["enabled"] == DEFAULT_PAYLOAD["enabled"]
    assert resolved["persist_accepts_to_graph"] is False


def test_resolve_payload_explicit_values_win():
    resolved = resolve_payload({
        "enabled": False,
        "aggressiveness": "aggressive",
        "persist_accepts_to_graph": True,
    })
    assert resolved == {
        "enabled": False,
        "aggressiveness": "aggressive",
        "persist_accepts_to_graph": True,
    }


def test_resolve_payload_drops_explicit_none_values():
    """``None`` for an optional field means "use the default", not "set to None"."""
    resolved = resolve_payload({"aggressiveness": None})
    assert resolved["aggressiveness"] == DEFAULT_PAYLOAD["aggressiveness"]


def test_validate_rejects_removed_command_hint_field():
    """The canonical v1 command text is fixed by product contract.

    Settings may tune wording, but they must not replace
    ``graph turn-correction suggest ... --json`` with an arbitrary wrapper.
    """
    with pytest.raises(SchemaValidationError):
        TurnCorrectionSettingsV1.validate(
            {"command_hint": "my-wrapper turn-correction --json"}
        )


# ── ops round-trip ───────────────────────────────────────────


def test_add_and_read_round_trip(graph_db_env):
    """Real Settings round-trip via the ops layer.

    The Setting is keyed by ``workspace.id`` so a workspace named
    "enterprise-ng" reads back its own row (and only its own row)
    against the registered schema.
    """
    sid = ops.add_setting(
        SET_ID, SCHEMA_REVISION,
        "enterprise-ng",
        {"aggressiveness": "aggressive", "enabled": True},
     org=ops.CALLER_ORG)
    assert sid

    members = ops.read_set(SET_ID, org=ops.CALLER_ORG)
    assert len(members.members) == 1
    member = members.members[0]
    assert member.id == sid
    assert member.key == "enterprise-ng"
    # Resolution completes the payload from the schema's declared defaults,
    # so what was written is present and what was omitted is filled.
    assert member.payload == {**DEFAULT_PAYLOAD,
                              "aggressiveness": "aggressive", "enabled": True}


def test_upsert_by_key_replaces_existing_row(graph_db_env):
    """An operator iterating on the Setting should not stack rows."""
    sid_a = ops.upsert_by_key(
        SET_ID, SCHEMA_REVISION, "enterprise-ng",
        {"aggressiveness": "balanced"},
     org=ops.CALLER_ORG)
    sid_b = ops.upsert_by_key(
        SET_ID, SCHEMA_REVISION, "enterprise-ng",
        {"aggressiveness": "off"},
     org=ops.CALLER_ORG)
    assert sid_a == sid_b
    members = ops.read_set(SET_ID, org=ops.CALLER_ORG)
    assert len(members.members) == 1
    assert members.members[0].payload["aggressiveness"] == "off"


def test_multiple_workspaces_are_independent(graph_db_env):
    """Each workspace gets its own Setting row, keyed by workspace.id."""
    ops.upsert_by_key(SET_ID, SCHEMA_REVISION, "ws-a", {"enabled": True}, org=ops.CALLER_ORG)
    ops.upsert_by_key(SET_ID, SCHEMA_REVISION, "ws-b", {"enabled": False}, org=ops.CALLER_ORG)
    members = ops.read_set(SET_ID, org=ops.CALLER_ORG)
    by_key = {m.key: m.payload["enabled"] for m in members.members}
    assert by_key == {"ws-a": True, "ws-b": False}


def test_add_setting_rejects_invalid_payload(graph_db_env):
    """Validation runs at write time, not just at read time."""
    with pytest.raises(SchemaValidationError):
        ops.add_setting(
            SET_ID, SCHEMA_REVISION, "ws", {"aggressiveness": "yolo"},
         org=ops.CALLER_ORG)


def test_resolution_fills_what_a_partial_setting_omitted(graph_db_env):
    """A default belongs to the schema, so every reader gets the same one.

    An operator writes only the knob they care about. Resolution completes
    the rest from the declared defaults, which is what lets a consumer read
    ``payload["aggressiveness"]`` instead of restating a fallback that could
    disagree with the schema's.
    """
    ops.upsert_by_key(SET_ID, SCHEMA_REVISION, "partial",
                      {"aggressiveness": "off"}, org=ops.CALLER_ORG)
    payload = ops.read_set(SET_ID, org=ops.CALLER_ORG).members[0].payload

    assert payload["aggressiveness"] == "off", "what was written wins"
    for name, default in DEFAULT_PAYLOAD.items():
        if name != "aggressiveness":
            assert payload[name] == default
