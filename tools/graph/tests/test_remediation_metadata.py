"""Data-only remediation declarations and their non-executing registry."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

import pytest

from agents.env_sources import (
    parse_capability_env_source,
    parse_workspace_env_source,
)
from tools.graph import settings_ops
from tools.graph.remediation import (
    RemediationSpec,
    get_remediation,
    list_remediations,
    remediation_registry_digest,
    register_remediation,
    validate_registered_ref,
)
from tools.graph.schemas.registry import (
    RemediationRef,
    SchemaValidationError,
    SettingSchema,
    field,
)
from tools.dashboard.server import _finding_json


PATH_REF = {"id": "workspace.declared-path.v1", "params": {}}


def test_typed_and_legacy_fields_normalize_to_the_same_plain_data():
    class Typed(SettingSchema):
        internal = True
        path: str = field(
            description="path", remediation=RemediationRef(**PATH_REF),
        )

    class Legacy(SettingSchema):
        internal = True
        _field_metadata = {
            "path": {
                "type": "string", "description": "path",
                "remediation": PATH_REF,
            },
        }

    assert Typed._field_metadata["path"]["remediation"] == PATH_REF
    assert Legacy._field_metadata["path"]["remediation"] == PATH_REF
    assert Typed._field_metadata["path"]["remediation"] is not PATH_REF
    assert Typed.export_json_schema()["properties"]["path"]["remediation"] == PATH_REF


def test_internal_element_schema_preserves_remediation_metadata():
    class Element(SettingSchema):
        internal = True
        path: str = field(
            description="path", remediation=RemediationRef(**PATH_REF),
        )

    class Outer(SettingSchema):
        internal = True
        rows: list = field(description="rows", element=Element)

    assert Outer._element_schemas["rows"] is Element
    assert Outer._field_metadata["rows"]["element"]["path"]["remediation"] == PATH_REF


@pytest.mark.parametrize(
    "bad_id",
    [
        "Workspace.path.v1", "workspace_path.v1", " workspace.path.v1",
        "workspace..path.v1", "1workspace.path.v1", "workspace.path.v0",
        "workspace.path", "workspace.path.v1 ",
    ],
)
def test_remediation_id_grammar_is_exact_and_has_no_normalization(bad_id):
    with pytest.raises(SchemaValidationError, match="must match"):
        RemediationRef(bad_id)


def test_rejected_parameter_value_never_appears_in_the_error():
    sentinel = "SENTINEL-PLAINTEXT-MUST-NOT-TRAVEL"
    with pytest.raises(SchemaValidationError) as caught:
        RemediationRef(
            "workspace.declared-path.v1",
            {"secret_value": sentinel},
        )
    assert sentinel not in str(caught.value)

    finding = settings_ops.CheckFinding(
        "address", "missing", "detail", "frame",
        remediation_id="workspace.declared-path.v1",
        remediation_params={"secret_value": sentinel},
    )
    serialized = _finding_json(finding)
    assert sentinel not in str(serialized)
    assert serialized["remediation_id"] == ""
    assert serialized["remediation_params"] == {}


def test_unknown_semantic_id_remains_structurally_valid_for_planner_fallback():
    ref = RemediationRef("workspace.future-action.v1")
    assert ref.id == "workspace.future-action.v1"
    assert validate_registered_ref({"id": ref.id, "params": ref.params}) == (
        "unknown remediation id 'workspace.future-action.v1'",
    )
    assert validate_registered_ref({
        "id": "workspace.declared-path.v1", "params": {"hint": "path"},
    }) == (
        "unknown remediation parameter(s) ['hint'] for "
        "workspace.declared-path.v1",
    )


def test_registry_is_six_public_non_executing_source_families_and_stable_digest():
    expected = {
        "workspace.env-from-host.legacy.v1",
        "workspace.env.credential.v1",
        "capability.env-binding.v1",
        "workspace.declared-path.v1",
        "capability.install-chain.v1",
        "repository.host-auth.v1",
    }
    assert {spec.id for spec in list_remediations()} == expected
    assert remediation_registry_digest() == remediation_registry_digest()
    for remediation_id in expected:
        spec = get_remediation(remediation_id)
        assert spec is not None
        assert spec.discovery_provider_id is None
        assert spec.preview_builder_id is None
        assert spec.executor_id is None
        assert "callable" not in spec.public_dict()


def test_registry_refuses_malformed_specs_and_duplicate_ids(monkeypatch):
    import tools.graph.remediation as remediation

    with pytest.raises((SchemaValidationError, ValueError)):
        RemediationSpec(
            id="not_valid", supported_finding_kinds=("missing",),
            parameter_types={}, action_type="manual", label="Bad",
            description="Bad ID", input_schema={}, discovery_provider_id=None,
            preview_builder_id=None, executor_id=None,
            required_authority="manual", verification="recheck",
        )

    monkeypatch.setattr(remediation, "_REGISTRY", {})
    spec = RemediationSpec(
        id="workspace.test.v1", supported_finding_kinds=("missing",),
        parameter_types={}, action_type="manual", label="Test",
        description="Test contract", input_schema={}, discovery_provider_id=None,
        preview_builder_id=None, executor_id=None,
        required_authority="manual", verification="recheck",
    )
    register_remediation(spec)
    with pytest.raises(ValueError, match="duplicate remediation id"):
        register_remediation(spec)


def test_shared_parser_keeps_workspace_and_capability_grammars_distinct():
    assert parse_workspace_env_source("credential:github.token").kind == "credential"
    assert parse_workspace_env_source("host:8080").kind == "literal"
    assert parse_workspace_env_source("file:///tmp/example").kind == "literal"

    host = parse_capability_env_source("host:GH_TOKEN")
    file_source = parse_capability_env_source("file:/tmp/a:b:GH_TOKEN")
    credential = parse_capability_env_source("credential:github.token")
    assert (host.kind, host.variable, host.valid) == ("host", "GH_TOKEN", True)
    assert (file_source.kind, file_source.locator, file_source.variable) == (
        "file", "/tmp/a:b", "GH_TOKEN",
    )
    assert (credential.kind, credential.locator, credential.valid) == (
        "credential", "github.token", True,
    )


def test_reference_and_custom_hook_findings_copy_structural_metadata(monkeypatch):
    class Target(SettingSchema):
        set_id = "probe.remediation.target"
        schema_revision = 1
        value: str = field(description="value")

    class Referrer(SettingSchema):
        set_id = "probe.remediation.referrer"
        schema_revision = 1
        target: str = field(
            description="target", references=Target.set_id,
            remediation=RemediationRef("workspace.future-action.v1"),
        )

    @dataclass(frozen=True)
    class Issue:
        kind: str = "missing_path"
        detail: str = "missing"
        field: str = "path"
        subject: str = "/missing"
        looked_in: str = "test frame"
        remediation_id: str = "workspace.declared-path.v1"
        remediation_params: dict = dataclass_field(default_factory=dict)

    class Hook(SettingSchema):
        set_id = "probe.remediation.hook"
        schema_revision = 1
        path: str = field(description="path")

        @classmethod
        def readiness_findings(cls, **_kwargs):
            return (Issue(),)

    rows = {
        (Referrer.set_id, "ref"): {
            "schema_revision": 1, "payload": {"target": "absent"},
        },
        (Hook.set_id, "hook"): {
            "schema_revision": 1, "payload": {"path": "/missing"},
        },
    }
    monkeypatch.setattr(
        settings_ops, "read_set_key",
        lambda set_id, key, **_kwargs: rows.get((set_id, key)),
    )
    monkeypatch.setattr(settings_ops, "rows_keyed_by", lambda *_a, **_k: [])
    monkeypatch.setattr(settings_ops, "_owned_but_unreadable", lambda *_a: None)

    ref_finding = settings_ops.check_setting(Referrer.set_id, "ref", org="acme")[0]
    hook_finding = settings_ops.check_setting(Hook.set_id, "hook", org="acme")[0]

    assert ref_finding.remediation_id == "workspace.future-action.v1"
    assert ref_finding.remediation_params == {}
    assert hook_finding.remediation_id == "workspace.declared-path.v1"
    assert hook_finding.remediation_params == {}
