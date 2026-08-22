"""Tests for the capability layer schemas.

Covers the four schemas introduced by `auto-0ase8`:

* `autonomy.capability.contract#1`
* `autonomy.capability.impl#1`
* `autonomy.org.capability.install#1`
* `autonomy.workspace.capability.enable#1`

Plus a resolution-path test that wires a contract -> impl -> org install
-> workspace enable through a minimal in-memory resolver, proving the
data model is coherent before later beads add runtime behavior.

Spec: graph://86e04207-a25 (Workspace Capability Layer).
"""

from __future__ import annotations

import copy

import pytest

from tools.graph.schemas import capability_contract, capability_impl
from tools.graph.schemas import org_capability_install, workspace_capability_enable
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    validate_payload,
)


# ── Fixtures ─────────────────────────────────────────────────


def _issue_tracker_v1() -> dict:
    return {
        "name": "issue_tracker",
        "version": 1,
        "summary": "Read and update issue/ticket state.",
        "ops": [
            {
                "name": "read",
                "summary": "Read a single ticket by key.",
                "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}},
                "output_schema": {"type": "object"},
            },
            {
                "name": "comment",
                "summary": "Append a comment to a ticket.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "create",
                "summary": "Create a new ticket from a payload.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "search",
                "summary": "Search tickets matching a query.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        ],
    }


def _source_control_v1() -> dict:
    return {
        "name": "source_control",
        "version": 1,
        "summary": "Branch and commit state.",
        "ops": [
            {
                "name": "branch_status",
                "summary": "Status of a named branch.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "commit_stack",
                "summary": "Commits ahead of base for a branch.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "integrated_diff",
                "summary": "Diff vs. integration base.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        ],
    }


def _autonomy_github_v1() -> dict:
    """v1 GitHub fixture.

    Per graph://86e04207-a25 the v1 capability shape collapses review and
    merge-gate concerns under ``source_control``. ``change_review`` and
    ``merge_gates`` are not separate top-level contracts in v1; the nested
    op inventory for ``source_control@1`` is finalized in a later bead.
    """
    return {
        "name": "autonomy/github",
        "version": 1,
        "implements": [
            {"contract": "source_control", "version": 1},
        ],
        "delivery_mode": "image_baked",
        "package_root": "agents/capabilities/github",
        "probe": {"kind": "command", "entrypoint": "gh auth status"},
        "required_env": ["GH_TOKEN"],
        "skill_path": "agents/capabilities/github/SKILL.md",
        "primer_path": "agents/capabilities/github/primer.md",
    }


def _autonomy_jira_v1() -> dict:
    return {
        "name": "autonomy/jira",
        "version": 1,
        "implements": [
            {"contract": "issue_tracker", "version": 1},
        ],
        "delivery_mode": "mounted_tools",
        "package_root": "agents/capabilities/jira",
        "probe": {"kind": "command", "entrypoint": "jira-read --probe"},
        "required_env": ["JIRA_EMAIL", "JIRA_BASE_URL"],
        "required_secret_files": ["/run/secrets/jira_token"],
        "tool_paths": ["agents/capabilities/jira/tools"],
        "skill_path": "agents/capabilities/jira/SKILL.md",
        "primer_path": "agents/capabilities/jira/primer.md",
    }


def _org_install_jira() -> dict:
    return {
        "contract": "issue_tracker",
        "contract_version": 1,
        "implementation": "autonomy/jira",
        "implementation_version": 1,
        "env_bindings": {
            "JIRA_EMAIL": "artifact:org/jira_email",
            "JIRA_BASE_URL": "artifact:org/jira_base_url",
        },
        "secret_file_bindings": {
            "/run/secrets/jira_token": "artifact:org/jira_token",
        },
    }


def _workspace_enable_issue_tracker() -> dict:
    return {
        "contract": "issue_tracker",
        "contract_version": 1,
        "enabled": True,
    }


# ── Contract schema ──────────────────────────────────────────


def test_contract_v1_is_a_flat_base_not_a_variant():
    """``capability_contract#1`` is the meta-shape every contract Setting
    must fit. Contract families (``issue_tracker``, ``source_control``,
    ...) live as payload ``name`` values rather than pre-enumerated
    variant subschemas — so the class is a direct ``SettingSchema``
    base with no variants and no variant slug.

    If a future bead introduces variant subclasses for, say, codegen
    consumers, that decision should land deliberately (and update this
    test). Pin the current shape so accidental drift is caught.
    """
    assert capability_contract.CapabilityContractV1._variant_slug is None
    assert capability_contract.CapabilityContractV1._variants == {}


def test_contract_v1_field_metadata_keys_match_legacy_dict_form():
    """The typed-field migration (Bead 3C) must produce the same set of
    ``_field_metadata`` keys as the pre-migration declaration so existing
    consumers (``export_json_schema``, codegen) keep seeing the same
    surface.
    """
    expected = {"name", "version", "summary", "ops", "notes", "ui_hints"}
    assert set(capability_contract.CapabilityContractV1._field_metadata) == expected


def test_contract_issue_tracker_v1_validates():
    validate_payload(
        capability_contract.SET_ID,
        capability_contract.SCHEMA_REVISION,
        _issue_tracker_v1(),
    )


def test_contract_source_control_v1_validates():
    validate_payload(
        capability_contract.SET_ID,
        capability_contract.SCHEMA_REVISION,
        _source_control_v1(),
    )


def test_contract_duplicate_op_names_fail():
    payload = _issue_tracker_v1()
    payload["ops"].append(payload["ops"][0])  # duplicate "read"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_contract.SET_ID,
            capability_contract.SCHEMA_REVISION,
            payload,
        )
    assert "duplicate op name" in str(ei.value)


def test_contract_missing_ops_fails():
    payload = _issue_tracker_v1()
    del payload["ops"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_contract.SET_ID,
            capability_contract.SCHEMA_REVISION,
            payload,
        )
    assert "ops" in str(ei.value)


def test_contract_non_integer_version_fails():
    payload = _issue_tracker_v1()
    payload["version"] = "1"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_contract.SET_ID,
            capability_contract.SCHEMA_REVISION,
            payload,
        )
    assert "version" in str(ei.value)


def test_contract_unknown_top_level_field_fails():
    payload = _issue_tracker_v1()
    payload["bogus"] = True
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_contract.SET_ID,
            capability_contract.SCHEMA_REVISION,
            payload,
        )
    assert "unknown field" in str(ei.value)


def test_contract_name_must_be_snake_case():
    payload = _issue_tracker_v1()
    payload["name"] = "IssueTracker"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_contract.SET_ID,
            capability_contract.SCHEMA_REVISION,
            payload,
        )
    assert "snake_case" in str(ei.value)


def test_contract_op_name_must_be_snake_case():
    payload = _issue_tracker_v1()
    payload["ops"][0]["name"] = "ReadIssue"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_contract.SET_ID,
            capability_contract.SCHEMA_REVISION,
            payload,
        )
    assert "snake_case" in str(ei.value)


# ── Implementation schema ───────────────────────────────────


def test_impl_autonomy_github_v1_validates():
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        _autonomy_github_v1(),
    )


def test_impl_autonomy_jira_v1_validates():
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        _autonomy_jira_v1(),
    )


def test_impl_invalid_delivery_mode_fails():
    payload = _autonomy_github_v1()
    payload["delivery_mode"] = "ftp"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "delivery_mode" in str(ei.value)


def test_impl_absolute_package_root_fails():
    payload = _autonomy_github_v1()
    payload["package_root"] = "/opt/capabilities/github"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "repo-local" in str(ei.value)


def test_impl_missing_probe_entrypoint_fails():
    payload = _autonomy_github_v1()
    del payload["probe"]["entrypoint"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "entrypoint" in str(ei.value)


def test_impl_requires_at_least_one_contract_ref():
    payload = _autonomy_github_v1()
    payload["implements"] = []
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "implements" in str(ei.value)


def test_impl_required_env_must_be_string_list():
    payload = _autonomy_github_v1()
    payload["required_env"] = [123]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "required_env" in str(ei.value)


def test_impl_unknown_top_level_field_fails():
    payload = _autonomy_github_v1()
    payload["surprise"] = 1
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "unknown field" in str(ei.value)


# ── Path safety (repo-local rule) ────────────────────────────
#
# All file/path fields on `autonomy.capability.impl#1` must point inside
# the repo-relative capability tree. Absolute paths, parent traversal
# (`..`), and degenerate values (empty string, `.`) are rejected before
# any later runtime trusts the metadata for mounts or reads.


@pytest.mark.parametrize(
    "bad_value",
    [
        "/opt/capabilities/github",          # absolute
        "../outside",                        # leading parent traversal
        "agents/../outside",                 # parent traversal mid-path
        "agents/capabilities/github/../..",  # exits the repo via traversal
        "",                                  # empty
        ".",                                 # degenerate (resolves to repo root)
        "agents/./capabilities/../..",       # normalizes outside the repo
    ],
)
def test_impl_package_root_rejects_repo_escape(bad_value):
    payload = _autonomy_github_v1()
    payload["package_root"] = bad_value
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "package_root" in str(ei.value)


@pytest.mark.parametrize(
    "bad_value",
    [
        "/etc/passwd",
        "../SKILL.md",
        "../../primer.md",
        "agents/../etc/passwd",
        "",
    ],
)
def test_impl_skill_path_rejects_repo_escape(bad_value):
    payload = _autonomy_github_v1()
    payload["skill_path"] = bad_value
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "skill_path" in str(ei.value)


@pytest.mark.parametrize(
    "bad_value",
    [
        "/etc/primer.md",
        "../../primer.md",
        "agents/capabilities/../../primer.md",
    ],
)
def test_impl_primer_path_rejects_repo_escape(bad_value):
    payload = _autonomy_github_v1()
    payload["primer_path"] = bad_value
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "primer_path" in str(ei.value)


def test_impl_tool_paths_rejects_repo_escape_in_any_entry():
    """One bad entry in a list of tool paths must fail the whole payload."""
    payload = _autonomy_jira_v1()
    payload["tool_paths"] = [
        "agents/capabilities/jira/tools",  # fine
        "../oops",                         # repo escape
    ]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "tool_paths" in str(ei.value)


def test_impl_tool_paths_rejects_absolute_entry():
    payload = _autonomy_jira_v1()
    payload["tool_paths"] = ["/opt/jira-tools"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "tool_paths" in str(ei.value)


def test_impl_tool_paths_rejects_entries_outside_package_root():
    """An external path would become a child of the read-only package mount."""
    payload = _autonomy_jira_v1()
    payload["tool_paths"] = ["tools/shared_jira_runtime"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    message = str(ei.value)
    assert "package_root" in message
    assert "tool_target" in message


# ── tool_target / command-surface (auto-1webn.2) ────────────
#
# `tool_paths` only declares which repo-local subtrees the capability
# brings along — it cannot say where the tool bundle should land inside
# the container or which commands should land on PATH. The Jira worked
# example needs both: `/opt/jira-tools` mount + `jira-read` etc.
# `tool_target` is the explicit substrate field that closes that gap.


def _jira_with_tool_target() -> dict:
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        "target": "/opt/jira-tools",
        "expose_commands": [
            "jira-read",
            "jira-comment",
            "jira-create",
            "jira-createmeta",
        ],
    }
    return payload


def test_impl_tool_target_jira_validates():
    """The Jira worked example: package root + /opt/jira-tools + jira-* commands."""
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        _jira_with_tool_target(),
    )


def test_impl_tool_target_without_expose_commands_validates():
    """`expose_commands` is optional — bundles without command shims still validate."""
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        "target": "/opt/jira-tools",
    }
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        payload,
    )


def test_impl_tool_target_must_be_object():
    payload = _autonomy_jira_v1()
    payload["tool_target"] = ["agents/capabilities/jira/tools", "/opt/jira-tools"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "tool_target" in str(ei.value)


def test_impl_tool_target_relative_target_fails():
    """The container target must be absolute — relative paths cannot become a stable mount."""
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        "target": "opt/jira-tools",
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "target" in str(ei.value)


def test_impl_tool_target_traversal_in_target_fails():
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        "target": "/opt/../etc/jira-tools",
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "target" in str(ei.value)


def test_impl_tool_target_repo_escape_in_source_fails():
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "../outside",
        "target": "/opt/jira-tools",
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "source" in str(ei.value)


def test_impl_tool_target_command_with_slash_fails():
    """expose_commands entries must be PATH command names, not paths."""
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        "target": "/opt/jira-tools",
        "expose_commands": ["bin/jira-read"],
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "expose_commands" in str(ei.value)


def test_impl_tool_target_unknown_subfield_fails():
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        "target": "/opt/jira-tools",
        "magic": True,
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "tool_target" in str(ei.value)


def test_impl_tool_target_missing_required_subfield_fails():
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        # missing target
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "target" in str(ei.value)


def test_impl_tool_target_empty_command_name_fails():
    payload = _autonomy_jira_v1()
    payload["tool_target"] = {
        "source": "agents/capabilities/jira/tools",
        "target": "/opt/jira-tools",
        "expose_commands": [""],
    }
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            capability_impl.SET_ID,
            capability_impl.SCHEMA_REVISION,
            payload,
        )
    assert "expose_commands" in str(ei.value)


def test_impl_normal_repo_local_paths_validate():
    """The reference fixtures (with deep but legal paths) still validate."""
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        _autonomy_github_v1(),
    )
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        _autonomy_jira_v1(),
    )

    # Multi-entry tool_paths with deeply-nested but legal repo-local
    # entries must still validate.
    payload = _autonomy_jira_v1()
    payload["tool_paths"] = [
        "agents/capabilities/jira/tools",
        "agents/capabilities/jira/tools/sub",
    ]
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        payload,
    )


# ── GitHub v1 contract shape ─────────────────────────────────
#
# graph://86e04207-a25 — the v1 model collapses review and merge-gate
# concerns under `source_control`. The placeholder GitHub impl must not
# advertise `change_review` or `merge_gates` as separate top-level
# contracts for the v1 shape; tests assert that the placeholder fixture
# matches the agreed v1 shape.


def test_github_v1_implements_source_control_only():
    payload = _autonomy_github_v1()
    declared = {ref["contract"] for ref in payload["implements"]}
    assert "source_control" in declared
    assert "change_review" not in declared, (
        "GitHub v1 must not advertise change_review as a separate top-level "
        "contract — review concerns nest under source_control@1"
    )
    assert "merge_gates" not in declared, (
        "GitHub v1 must not advertise merge_gates as a separate top-level "
        "contract — gate concerns nest under source_control@1"
    )


def test_github_manifest_file_matches_v1_shape():
    """The on-disk manifest stub must validate and match the v1 shape."""
    import json
    from pathlib import Path

    manifest_path = (
        Path(__file__).resolve().parents[3]
        / "agents"
        / "capabilities"
        / "github"
        / "manifest.json"
    )
    payload = json.loads(manifest_path.read_text())
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        payload,
    )
    declared = {ref["contract"] for ref in payload["implements"]}
    assert declared == {"source_control"}, (
        f"GitHub manifest must implement source_control only for v1; "
        f"got {sorted(declared)}"
    )


# ── Org install schema ──────────────────────────────────────


def test_org_install_validates():
    validate_payload(
        org_capability_install.SET_ID,
        org_capability_install.SCHEMA_REVISION,
        _org_install_jira(),
    )


def test_org_install_non_object_env_binding_fails():
    payload = _org_install_jira()
    payload["env_bindings"] = ["JIRA_EMAIL"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            org_capability_install.SET_ID,
            org_capability_install.SCHEMA_REVISION,
            payload,
        )
    assert "env_bindings" in str(ei.value)


def test_org_install_missing_implementation_ref_fails():
    payload = _org_install_jira()
    del payload["implementation"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            org_capability_install.SET_ID,
            org_capability_install.SCHEMA_REVISION,
            payload,
        )
    assert "implementation" in str(ei.value)


def test_org_install_unknown_field_fails():
    payload = _org_install_jira()
    payload["weird"] = True
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            org_capability_install.SET_ID,
            org_capability_install.SCHEMA_REVISION,
            payload,
        )
    assert "unknown field" in str(ei.value)


# ── Workspace enable schema ─────────────────────────────────


def test_workspace_enable_enabled_validates():
    validate_payload(
        workspace_capability_enable.SET_ID,
        workspace_capability_enable.SCHEMA_REVISION,
        _workspace_enable_issue_tracker(),
    )


def test_workspace_enable_disabled_validates():
    """Explicit ``enabled=false`` is allowed so a workspace can opt out."""
    payload = {
        "contract": "issue_tracker",
        "enabled": False,
    }
    validate_payload(
        workspace_capability_enable.SET_ID,
        workspace_capability_enable.SCHEMA_REVISION,
        payload,
    )


def test_workspace_enable_unpinned_contract_version_validates():
    payload = {
        "contract": "issue_tracker",
        "contract_version": None,
    }
    validate_payload(
        workspace_capability_enable.SET_ID,
        workspace_capability_enable.SCHEMA_REVISION,
        payload,
    )


def test_workspace_enable_invalid_enabled_type_fails():
    payload = _workspace_enable_issue_tracker()
    payload["enabled"] = "yes"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            workspace_capability_enable.SET_ID,
            workspace_capability_enable.SCHEMA_REVISION,
            payload,
        )
    assert "enabled" in str(ei.value)


def test_workspace_enable_invalid_override_shape_fails():
    payload = _workspace_enable_issue_tracker()
    payload["workspace_overrides"] = ["not", "an", "object"]
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            workspace_capability_enable.SET_ID,
            workspace_capability_enable.SCHEMA_REVISION,
            payload,
        )
    assert "workspace_overrides" in str(ei.value)


def test_workspace_enable_unknown_field_fails():
    payload = _workspace_enable_issue_tracker()
    payload["weird"] = 1
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(
            workspace_capability_enable.SET_ID,
            workspace_capability_enable.SCHEMA_REVISION,
            payload,
        )
    assert "unknown field" in str(ei.value)


# ── Registry round-trip ─────────────────────────────────────


def test_registry_resolves_all_four_schemas():
    """The four new schemas show up by their ``set_id#revision`` keys."""
    pairs = [
        (capability_contract.SET_ID, capability_contract.SCHEMA_REVISION),
        (capability_impl.SET_ID, capability_impl.SCHEMA_REVISION),
        (org_capability_install.SET_ID, org_capability_install.SCHEMA_REVISION),
        (workspace_capability_enable.SET_ID, workspace_capability_enable.SCHEMA_REVISION),
    ]
    for set_id, revision in pairs:
        cls = get_schema(set_id, revision)
        assert cls is not None, f"schema not registered: {set_id}#{revision}"
        assert cls.set_id == set_id
        assert cls.schema_revision == revision


# ── Resolution path ─────────────────────────────────────────


def _resolve_implementation(
    *,
    contracts: dict[tuple[str, int], dict],
    impls: dict[tuple[str, int], dict],
    org_installs: dict[str, dict],   # keyed by contract name
    workspace_enables: dict[str, dict],   # keyed by contract name
    contract_name: str,
    working_versions: dict[str, int],   # current working version per contract name
) -> dict | None:
    """Minimal resolver: given a contract name, walk the chain to pick an impl.

    Steps mirror the architecture note:

    1. Workspace must reference the contract and not be explicitly disabled.
    2. The contract version is taken from the workspace pin if present,
       otherwise the org-install pin, otherwise the current working
       version.
    3. The contract record at ``(name, version)`` must exist.
    4. The org install must point to an implementation that implements
       the resolved contract version.
    5. The implementation record must exist.

    Returns the implementation payload, or ``None`` if any step fails.
    """
    enable = workspace_enables.get(contract_name)
    if enable is None:
        return None
    if enable.get("enabled", True) is False:
        return None

    install = org_installs.get(contract_name)
    if install is None:
        return None

    contract_version = enable.get("contract_version")
    if contract_version is None:
        contract_version = install.get("contract_version")
    if contract_version is None:
        contract_version = working_versions.get(contract_name)
    if contract_version is None:
        return None

    if (contract_name, contract_version) not in contracts:
        return None

    impl_name = install["implementation"]
    impl_version = install["implementation_version"]
    impl = impls.get((impl_name, impl_version))
    if impl is None:
        return None

    # Implementation must declare it implements the resolved contract.
    declared = {(r["contract"], r["version"]) for r in impl["implements"]}
    if (contract_name, contract_version) not in declared:
        return None

    return impl


def test_resolution_path_contract_impl_install_enable_resolves():
    """Wire all four schema records together and resolve the chosen impl.

    This is the acceptance criterion: contract -> impl -> org install ->
    workspace enable can be walked coherently to determine the selected
    implementation for a workspace.
    """
    issue_tracker = _issue_tracker_v1()
    impl = _autonomy_jira_v1()
    install = _org_install_jira()
    enable = _workspace_enable_issue_tracker()

    # Validate every record so the resolver only sees well-formed data.
    validate_payload(
        capability_contract.SET_ID,
        capability_contract.SCHEMA_REVISION,
        issue_tracker,
    )
    validate_payload(
        capability_impl.SET_ID,
        capability_impl.SCHEMA_REVISION,
        impl,
    )
    validate_payload(
        org_capability_install.SET_ID,
        org_capability_install.SCHEMA_REVISION,
        install,
    )
    validate_payload(
        workspace_capability_enable.SET_ID,
        workspace_capability_enable.SCHEMA_REVISION,
        enable,
    )

    contracts = {(issue_tracker["name"], issue_tracker["version"]): issue_tracker}
    impls = {(impl["name"], impl["version"]): impl}
    org_installs = {install["contract"]: install}
    workspace_enables = {enable["contract"]: enable}
    working_versions = {issue_tracker["name"]: issue_tracker["version"]}

    resolved = _resolve_implementation(
        contracts=contracts,
        impls=impls,
        org_installs=org_installs,
        workspace_enables=workspace_enables,
        contract_name="issue_tracker",
        working_versions=working_versions,
    )
    assert resolved is not None
    assert resolved["name"] == "autonomy/jira"
    assert resolved["version"] == 1


def test_resolution_path_workspace_disable_blocks_resolution():
    """A workspace can opt out of an org-installed capability."""
    issue_tracker = _issue_tracker_v1()
    impl = _autonomy_jira_v1()
    install = _org_install_jira()
    enable = {"contract": "issue_tracker", "enabled": False}

    validate_payload(
        workspace_capability_enable.SET_ID,
        workspace_capability_enable.SCHEMA_REVISION,
        enable,
    )

    resolved = _resolve_implementation(
        contracts={(issue_tracker["name"], issue_tracker["version"]): issue_tracker},
        impls={(impl["name"], impl["version"]): impl},
        org_installs={install["contract"]: install},
        workspace_enables={enable["contract"]: enable},
        contract_name="issue_tracker",
        working_versions={issue_tracker["name"]: issue_tracker["version"]},
    )
    assert resolved is None


def test_resolution_path_unpinned_workspace_uses_install_version():
    """When the workspace omits ``contract_version``, the install pin wins."""
    issue_tracker = _issue_tracker_v1()
    impl = _autonomy_jira_v1()
    install = _org_install_jira()
    enable = {"contract": "issue_tracker"}  # no contract_version

    resolved = _resolve_implementation(
        contracts={(issue_tracker["name"], issue_tracker["version"]): issue_tracker},
        impls={(impl["name"], impl["version"]): impl},
        org_installs={install["contract"]: install},
        workspace_enables={enable["contract"]: enable},
        contract_name="issue_tracker",
        working_versions={issue_tracker["name"]: 99},   # ignored, install pins to 1
    )
    assert resolved is not None
    assert resolved["name"] == "autonomy/jira"


def test_resolution_path_implementation_not_implementing_contract_version_fails():
    """If the impl doesn't declare the resolved contract version, no match."""
    issue_tracker = _issue_tracker_v1()
    impl = _autonomy_jira_v1()
    # Mutate the impl to claim it implements a different version than the install.
    impl_v2 = copy.deepcopy(impl)
    impl_v2["implements"][0]["version"] = 2
    install = _org_install_jira()  # contract_version=1, implementation_version=1

    resolved = _resolve_implementation(
        contracts={(issue_tracker["name"], issue_tracker["version"]): issue_tracker},
        impls={(impl_v2["name"], impl_v2["version"]): impl_v2},
        org_installs={install["contract"]: install},
        workspace_enables={"issue_tracker": _workspace_enable_issue_tracker()},
        contract_name="issue_tracker",
        working_versions={issue_tracker["name"]: 1},
    )
    assert resolved is None


# ── impl rev 2: host_install (protocol 149705db-a39) ─────────


def _video_host_install() -> dict:
    return {
        "command": ["bash", "install/install.sh"],
        "cwd": "agents/capabilities/video",
        "fingerprint_files": ["agents/capabilities/video/install/ffmpeg.pin"],
        "timeout_seconds": 600,
        "success_marker": "bin/ffmpeg",
    }


def _autonomy_video_v1() -> dict:
    payload = _autonomy_github_v1()
    payload["name"] = "autonomy/video"
    payload["delivery_mode"] = "mounted_tools"
    payload["package_root"] = "agents/capabilities/video"
    payload["skill_path"] = "agents/capabilities/video/SKILL.md"
    payload["primer_path"] = "agents/capabilities/video/primer.md"
    del payload["required_env"]
    payload["host_install"] = _video_host_install()
    return payload


def test_impl_rev2_accepts_host_install():
    validate_payload(capability_impl.SET_ID, 2, _autonomy_video_v1())


def test_impl_rev2_validates_the_landed_video_manifest():
    """The real manifest on master is the acceptance payload."""
    import json
    from pathlib import Path

    manifest = json.loads(
        Path("agents/capabilities/video/manifest.json").read_text()
    )
    validate_payload(capability_impl.SET_ID, 2, manifest)
    assert manifest["tool_target"]["expose_commands"] == [
        "video-probe", "video-contact-sheet", "video-scene-detect",
        "video-convert", "ffmpeg", "ffprobe",
    ]


def test_impl_rev2_validates_the_landed_agent_test_manifest():
    """Agent Test's mounted command surface is a valid implementation."""
    import json
    from pathlib import Path

    manifest = json.loads(
        Path("agents/capabilities/agent_test/manifest.json").read_text()
    )
    validate_payload(capability_impl.SET_ID, 2, manifest)
    assert manifest["implements"] == [
        {"contract": "test_execution", "version": 1}
    ]
    assert manifest["tool_target"]["expose_commands"] == [
        "agent-test", "pytest", "py.test",
    ]
    contract = json.loads(
        Path("agents/capabilities/agent_test/contract.json").read_text()
    )
    validate_payload(capability_contract.SET_ID, 1, contract)
    assert {op["name"] for op in contract["ops"]} == {
        "run", "inspect", "statistics",
    }


def test_impl_rev2_minimal_host_install_validates():
    payload = _autonomy_video_v1()
    payload["host_install"] = {
        "command": ["npm", "install"],
        "fingerprint_files": ["agents/capabilities/video/install/ffmpeg.pin"],
    }
    validate_payload(capability_impl.SET_ID, 2, payload)


def test_impl_rev2_env_accepted():
    payload = _autonomy_video_v1()
    payload["host_install"]["env"] = {"PUPPETEER_SKIP_DOWNLOAD": "1"}
    validate_payload(capability_impl.SET_ID, 2, payload)


def test_impl_rev1_rows_still_validate_at_rev1():
    """Existing stored rows (github/jira shape) are untouched by rev 2."""
    validate_payload(capability_impl.SET_ID, 1, _autonomy_github_v1())


def test_impl_rev1_still_rejects_host_install():
    """The rev-1 contract is unchanged: host_install stays unknown there."""
    with pytest.raises(SchemaValidationError, match="unknown field"):
        validate_payload(capability_impl.SET_ID, 1, _autonomy_video_v1())


def test_impl_rev2_upconverts_rev1_payload():
    from tools.graph.schemas.registry import upconvert_chain

    chain = upconvert_chain(capability_impl.SET_ID, 1, 2)
    assert chain is not None and len(chain) == 1
    upconverted = chain[0](_autonomy_github_v1())
    validate_payload(capability_impl.SET_ID, 2, upconverted)


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda hi: hi.pop("command"), "command"),
        (lambda hi: hi.update(command=[]), "command"),
        (lambda hi: hi.update(command="bash install.sh"), "command"),
        (lambda hi: hi.update(command=["bash", 3]), "command"),
        (lambda hi: hi.pop("fingerprint_files"), "fingerprint_files"),
        (lambda hi: hi.update(fingerprint_files=[]), "fingerprint_files"),
        (lambda hi: hi.update(fingerprint_files=["/etc/passwd"]), "fingerprint_files"),
        (lambda hi: hi.update(fingerprint_files=["../escape"]), "fingerprint_files"),
        (lambda hi: hi.update(cwd="/abs/path"), "cwd"),
        (lambda hi: hi.update(cwd="../escape"), "cwd"),
        (lambda hi: hi.update(env={"K": 1}), "env"),
        (lambda hi: hi.update(env="X=1"), "env"),
        (lambda hi: hi.update(timeout_seconds=0), "timeout_seconds"),
        (lambda hi: hi.update(timeout_seconds=-5), "timeout_seconds"),
        (lambda hi: hi.update(timeout_seconds="600"), "timeout_seconds"),
        (lambda hi: hi.update(timeout_seconds=True), "timeout_seconds"),
        (lambda hi: hi.update(success_marker=""), "success_marker"),
        (lambda hi: hi.update(success_marker=7), "success_marker"),
        (lambda hi: hi.update(surprise_field=1), "unknown"),
        (lambda hi: None, "host_install"),  # replaced below with non-dict
    ],
)
def test_impl_rev2_rejects_malformed_host_install(mutation, match):
    payload = _autonomy_video_v1()
    result = mutation(payload["host_install"])
    if match == "host_install" and result is None:
        payload["host_install"] = ["not", "a", "dict"]
    with pytest.raises(SchemaValidationError, match=match):
        validate_payload(capability_impl.SET_ID, 2, payload)
