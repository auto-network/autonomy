"""Focused tests for workspace Setting composition helpers."""

from __future__ import annotations

import pytest

from agents.workspace_settings import (
    WorkspaceSettingsError,
    _workspace_from_setting,
)


def test_workspace_from_setting_defaults_harness_to_claude():
    workspace = _workspace_from_setting(
        {"name": "Autonomy", "image": "autonomy-agent:dashboard"},
        workspace_id="autonomy",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.harness == "claude"


def test_workspace_from_setting_reads_codex_harness():
    workspace = _workspace_from_setting(
        {
            "name": "Autonomy Codex",
            "image": "autonomy-agent:dashboard",
            "harness": "codex",
        },
        workspace_id="autonomy-codex",
        graph_project="autonomy",
        artifacts=(),
        mounts={},
    )
    assert workspace.harness == "codex"


def test_workspace_from_setting_rejects_invalid_harness():
    with pytest.raises(WorkspaceSettingsError, match="invalid harness"):
        _workspace_from_setting(
            {
                "name": "Broken",
                "image": "autonomy-agent:dashboard",
                "harness": "bogus",
            },
            workspace_id="broken",
            graph_project="autonomy",
            artifacts=(),
            mounts={},
        )
