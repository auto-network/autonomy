"""``autonomy.workspace.provision#1`` and ``autonomy.workspace.image-build#1``.

The contract the launcher and the host image builder both trust: content
fields validated at the write boundary, the image name derived from the
key and never stored, build outcomes exactly one of success or failure.
"""

from __future__ import annotations

import pytest

from tools.graph.schemas import workspace_image_build, workspace_provision
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    validate_payload,
)

PROVISION = workspace_provision.SET_ID
IMAGE_BUILD = workspace_image_build.SET_ID
SHA = "a" * 64


def test_both_schemas_register_at_import():
    assert get_schema(PROVISION, 1) is workspace_provision.WorkspaceProvisionV1
    assert get_schema(IMAGE_BUILD, 1) \
        is workspace_image_build.WorkspaceImageBuildV1


def test_provision_home_and_band():
    cls = workspace_provision.WorkspaceProvisionV1
    assert cls._home == "organization"
    # max=curated is what structurally closes the federated injection
    # channel; a loosened band is a security regression, not a tidy-up.
    assert cls._publication_band == ("raw", "curated")


def test_image_build_is_machine_homed():
    cls = workspace_image_build.WorkspaceImageBuildV1
    assert cls._home == "machine"
    assert cls._publication_band == ("raw", "curated")


def test_provision_accepts_each_content_field_alone():
    validate_payload(PROVISION, 1, {"startup_script": "#!/bin/bash\necho hi"})
    validate_payload(PROVISION, 1, {"dockerfile": "FROM autonomy-session\n"})
    validate_payload(PROVISION, 1, {
        "startup_script": "echo hi",
        "dockerfile": "FROM x",
        "description": "both",
    })


def test_provision_rejects_no_content():
    with pytest.raises(SchemaValidationError, match="at least one"):
        validate_payload(PROVISION, 1, {"description": "empty holder"})
    with pytest.raises(SchemaValidationError, match="at least one"):
        validate_payload(PROVISION, 1, {"startup_script": ""})


def test_provision_rejects_dockerfile_without_from():
    with pytest.raises(SchemaValidationError, match="no FROM"):
        validate_payload(PROVISION, 1, {"dockerfile": "RUN echo hi"})


def test_provision_rejects_undeclared_field():
    # The image name derives from org + key; a stored name would be the
    # key-in-payload failure the settings guide bans.
    with pytest.raises(SchemaValidationError):
        validate_payload(PROVISION, 1, {
            "dockerfile": "FROM x", "image": "autonomy/dev",
        })


def test_image_build_success_row():
    validate_payload(IMAGE_BUILD, 1, {
        "content_hash": SHA,
        "built_at": "2026-08-28T00:00:00Z",
        "digest": "sha256:abc",
    })


def test_image_build_failure_row():
    validate_payload(IMAGE_BUILD, 1, {
        "content_hash": SHA,
        "built_at": "2026-08-28T00:00:00Z",
        "error": "FROM image not found",
    })


def test_image_build_rejects_both_and_neither_outcome():
    base = {"content_hash": SHA, "built_at": "2026-08-28T00:00:00Z"}
    with pytest.raises(SchemaValidationError, match="exactly one"):
        validate_payload(IMAGE_BUILD, 1, dict(base))
    with pytest.raises(SchemaValidationError, match="exactly one"):
        validate_payload(IMAGE_BUILD, 1, {
            **base, "digest": "sha256:abc", "error": "also failed?",
        })


def test_image_build_rejects_malformed_hash():
    with pytest.raises(SchemaValidationError, match="64 lowercase hex"):
        validate_payload(IMAGE_BUILD, 1, {
            "content_hash": "abc123",
            "built_at": "2026-08-28T00:00:00Z",
            "digest": "sha256:abc",
        })
