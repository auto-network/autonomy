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
    return {
        "name": "autonomy/github",
        "version": 1,
        "implements": [
            {"contract": "source_control", "version": 1},
            {"contract": "change_review", "version": 1},
            {"contract": "merge_gates", "version": 1},
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
