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


class TestMountVisibility:
    """``visibility`` — access scope, added to rev 2 rather than minting rev 3.

    The registry does not downconvert (``upconvert_chain`` returns None when
    from_rev > to_rev) and ``workspace_settings.load_mounts`` pins
    ``target_revision=2``, so a rev-3 row would drop for its only consumer.
    An optional field with a safe default is compatible both directions.
    Operator decision graph://89535205-2b6, 2026-08-29.
    """

    def _subpath_row(self, **over) -> dict:
        row = {
            "subpath": "anchore/anchore-enterprise",
            "container_path": "/nfs",
            "kind": "dir",
        }
        row.update(over)
        return row

    def test_legacy_row_without_visibility_means_machine(self):
        """The narrowest scope, not the broadest — what every pre-existing
        row means. Materializes through the model, so consumers reading via
        read_set(model=...) never need a .get() fallback."""
        from tools.graph.schemas.mount import WorkspaceMountV2

        legacy = WorkspaceMountV2.model_validate({
            "host_path": "/home/op/data/harness",
            "container_path": "/opt/harness/",
        })
        assert legacy.visibility == "machine"
        modern = WorkspaceMountV2.model_validate(self._subpath_row())
        assert modern.visibility == "machine"

    @pytest.mark.parametrize("scope", ["machine", "personal", "organization"])
    def test_every_declared_scope_is_accepted(self, scope):
        from tools.graph.schemas.mount import WorkspaceMountV2

        m = WorkspaceMountV2.model_validate(self._subpath_row(visibility=scope))
        assert m.visibility == scope

    @pytest.mark.parametrize("bad", ["cross-org", "org", "public", "", "Machine"])
    def test_undeclared_scopes_are_refused(self, bad):
        """No cross-org value: a mount visible to two organizations is a
        contradiction in terms, not a scope."""
        from tools.graph.schemas.mount import WorkspaceMountV2

        with pytest.raises(Exception):
            WorkspaceMountV2.model_validate(self._subpath_row(visibility=bad))

    def test_registry_validation_accepts_visibility_at_rev_2(self):
        """enforce_declared_fields rejects any key the schema does not
        declare, so the field must be in _field_metadata too — not just on
        the Pydantic model."""
        from tools.graph.schemas.mount import MOUNT_SCHEMA_REVISION_2

        validate_payload(
            SET_ID, MOUNT_SCHEMA_REVISION_2,
            self._subpath_row(visibility="organization"),
        )

    def test_rev_1_row_upconverts_and_gains_the_default(self):
        """The 1->2 upconverter is identity; the default supplies meaning."""
        from tools.graph.schemas.mount import WorkspaceMountV2
        from tools.graph.schemas.registry import upconvert_payload

        rev1 = {
            "host_path": "/home/op/data/harness",
            "container_path": "/opt/harness",
            "mode": "ro",
            "required": True,
        }
        carried = upconvert_payload(SET_ID, 1, 2, rev1)
        assert carried is not None, "identity upconverter must be registered"
        assert WorkspaceMountV2.model_validate(carried).visibility == "machine"

    def test_rev_3_is_reachable_from_every_stored_revision(self):
        """Rev 3 (host_path deleted) registers with identity upconverters
        from both stored revisions, so no clean row is stranded; a
        straggler host_path row fails rev-3 validation by name instead of
        serving a machine path."""
        from tools.graph import schemas
        from tools.graph.schemas.mount import MOUNT_SCHEMA_REVISION_3

        assert schemas.get_schema(SET_ID, MOUNT_SCHEMA_REVISION_3) is not None
        assert schemas.upconvert_chain(SET_ID, 1, MOUNT_SCHEMA_REVISION_3)
        assert schemas.upconvert_chain(SET_ID, 2, MOUNT_SCHEMA_REVISION_3)
        with pytest.raises(Exception, match="host_path was deleted"):
            schemas.get_schema(SET_ID, MOUNT_SCHEMA_REVISION_3).validate({
                "host_path": "/home/op/data", "container_path": "/opt/x",
            })


def test_workspace_and_mount_are_pinned_to_raw():
    """Both sets are ``max="raw"`` — rows never leave the database owning them.

    Operator decision, 2026-08-29: a state is only worth allowing if some
    workflow needs it. For these two sets ``curated`` could do exactly one
    thing — outrank a ``raw`` row at the same key in the same database,
    since both states sit below the public surface and neither is readable
    from another org — and no key anywhere was using it. It bought nothing
    and cost deletion: ``graph set remove`` accepts only ``raw``, which is
    how 16 mount rows became undeletable.

    Widening this again is a real change, not a tidy-up: at ``curated`` a
    row becomes unremovable, and the band is what forecloses cross-database
    override/exclude on these sets.
    """
    from tools.graph.schemas import workspace as workspace_schema
    from tools.graph.schemas.mount import (
        MOUNT_SCHEMA_REVISION_2, SET_ID as MOUNT_SET_ID,
    )

    for cls in (get_schema(MOUNT_SET_ID, SCHEMA_REVISION),
                get_schema(MOUNT_SET_ID, MOUNT_SCHEMA_REVISION_2)):
        assert cls._publication_band == ("raw", "raw"), cls.__name__

    assert workspace_schema.WorkspaceV1._publication_band == ("raw", "raw")
