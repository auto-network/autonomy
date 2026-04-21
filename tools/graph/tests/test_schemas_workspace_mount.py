"""Tests for the ``autonomy.workspace.mount#1`` Setting schema.

Covers Pydantic validation (valid payload, absolute-path requirement,
mode enum), and registry round-trip (``get_schema`` resolves the adapter,
``validate_payload`` runs the adapter end-to-end).
"""

from __future__ import annotations

import pytest

from tools.graph import schemas
from tools.graph.schemas import mount as mount_module
from tools.graph.schemas.mount import (
    SET_ID,
    SCHEMA_REVISION,
    WorkspaceMountV1,
)
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    validate_payload,
)


def _valid_payload() -> dict:
    return {
        "host_path": "/home/op/data/vuln-diff-validation",
        "container_path": "/opt/vuln-diff",
        "mode": "ro",
        "description": "vuln-diff validation harness",
        "required": True,
    }


def test_workspacemountv1_accepts_valid_payload():
    m = WorkspaceMountV1.model_validate(_valid_payload())
    assert m.host_path == "/home/op/data/vuln-diff-validation"
    assert m.container_path == "/opt/vuln-diff"
    assert m.mode == "ro"
    assert m.required is True


def test_workspacemountv1_defaults_mode_and_required():
    m = WorkspaceMountV1.model_validate({
        "host_path": "/host/p",
        "container_path": "/ctr/p",
    })
    assert m.mode == "ro"
    assert m.required is True
    assert m.description is None


def test_workspacemountv1_rejects_non_absolute_container_path():
    payload = _valid_payload()
    payload["container_path"] = "relative/path"
    with pytest.raises(Exception) as ei:
        WorkspaceMountV1.model_validate(payload)
    assert "container_path must be absolute" in str(ei.value)


def test_workspacemountv1_rejects_non_absolute_host_path():
    payload = _valid_payload()
    payload["host_path"] = "relative/path"
    with pytest.raises(Exception) as ei:
        WorkspaceMountV1.model_validate(payload)
    assert "host_path must be absolute" in str(ei.value)


def test_workspacemountv1_rejects_invalid_mode():
    payload = _valid_payload()
    payload["mode"] = "rwx"
    with pytest.raises(Exception):
        WorkspaceMountV1.model_validate(payload)


def test_registry_resolves_adapter():
    """``get_schema('autonomy.workspace.mount', 1)`` returns the adapter class."""
    cls = get_schema(SET_ID, SCHEMA_REVISION)
    assert cls is not None
    # The adapter wraps the Pydantic model and exposes it as ``cls.model``.
    assert cls.model is WorkspaceMountV1


def test_registry_validate_payload_accepts_valid_dict():
    """``validate_payload`` runs the Pydantic model via the adapter."""
    validate_payload(SET_ID, SCHEMA_REVISION, _valid_payload())


def test_registry_validate_payload_rejects_relative_container_path():
    payload = _valid_payload()
    payload["container_path"] = "relative/only"
    with pytest.raises(SchemaValidationError) as ei:
        validate_payload(SET_ID, SCHEMA_REVISION, payload)
    assert "container_path must be absolute" in str(ei.value)


def test_registry_validate_payload_rejects_non_dict():
    with pytest.raises(SchemaValidationError):
        validate_payload(SET_ID, SCHEMA_REVISION, "not-a-dict")


def test_mount_module_exports_set_id_and_revision():
    """Consumer wiring depends on these module-level constants."""
    assert mount_module.SET_ID == "autonomy.workspace.mount"
    assert mount_module.SCHEMA_REVISION == 1
