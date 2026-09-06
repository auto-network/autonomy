"""The backup plugin's settings contracts.

The substrate enforces declarations (undeclared fields, required
fields, enums) on every write; these tests pin the cross-field rules
the declarations cannot express — the silent-success refusals — plus a
probe that the substrate's declaration enforcement is active for these
sets.
"""
from __future__ import annotations

import pytest

# Importing the module registers the three schemas with the registry.
from tools.dashboard.plugins.backup.entrypoints import schemas as S
from tools.graph.schemas.registry import (
    SchemaValidationError,
    validate_payload,
)


def _run(**kw) -> dict:
    base = {"verdict": "complete", "started_at": "2026-09-06T00:11:10+00:00"}
    base.update(kw)
    return base


def _drill(**kw) -> dict:
    base = {"verdict": "pass", "started_at": "2026-09-06T00:20:00+00:00",
            "checks": [{"name": "integrity", "status": "ok"}]}
    base.update(kw)
    return base


class TestRunVocabulary:
    def test_complete_run_validates(self):
        validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION, _run())

    def test_complete_run_refuses_failure_reasons(self):
        """The 2026-09-06 defect, encoded: 'complete' may never carry
        failures."""
        with pytest.raises(SchemaValidationError, match="silent-success"):
            validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION,
                             _run(failures=["auth.db missing"]))

    def test_failed_run_must_say_why(self):
        with pytest.raises(SchemaValidationError, match="must say why"):
            validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION,
                             _run(verdict="failed"))
        validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION,
                         _run(verdict="failed",
                              failures=["auth: MISSING required store"]))

    def test_substrate_enforcement_is_active(self):
        """One probe: an undeclared field is refused by the substrate's
        declaration layer, not our validate()."""
        with pytest.raises(SchemaValidationError):
            validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION,
                             _run(undeclared_field=1))

    def test_verdict_enum_enforced(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION,
                             _run(verdict="partial"))

    def test_store_rows_shape_enforced(self):
        validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION, _run(
            stores=[{"name": "orgs/autonomy.db", "status": "ok",
                     "bytes": 1557696512}]))
        with pytest.raises(SchemaValidationError):
            validate_payload(S.RUN_SET_ID, S.SCHEMA_REVISION,
                             _run(stores=[{"status": "ok"}]))  # name required


class TestDrillVocabulary:
    def test_pass_drill_validates(self):
        validate_payload(S.DRILL_SET_ID, S.SCHEMA_REVISION, _drill())

    def test_pass_requires_checks(self):
        with pytest.raises(SchemaValidationError, match="what it checked"):
            validate_payload(S.DRILL_SET_ID, S.SCHEMA_REVISION,
                             _drill(checks=[]))

    def test_pass_cannot_carry_failed_checks(self):
        with pytest.raises(SchemaValidationError, match="failed checks"):
            validate_payload(S.DRILL_SET_ID, S.SCHEMA_REVISION, _drill(
                checks=[{"name": "integrity", "status": "fail"}]))

    def test_running_has_no_finish(self):
        with pytest.raises(SchemaValidationError, match="no finish time"):
            validate_payload(S.DRILL_SET_ID, S.SCHEMA_REVISION, _drill(
                verdict="running", checks=[],
                finished_at="2026-09-06T00:30:00+00:00"))
        validate_payload(S.DRILL_SET_ID, S.SCHEMA_REVISION,
                         _drill(verdict="running", checks=[]))


class TestConfigBounds:
    def test_defaults_validate(self):
        validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION, {})

    def test_staleness_multiple_floor(self):
        with pytest.raises(SchemaValidationError, match="staleness_multiple"):
            validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                             {"staleness_multiple": 0.5})

    def test_daily_hour_range(self):
        with pytest.raises(SchemaValidationError, match="daily_hour"):
            validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                             {"daily_hour": 24})

    def test_retention_floor(self):
        with pytest.raises(SchemaValidationError, match="run_retention"):
            validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                             {"run_retention": 0})

    def test_schedule_owner_enum(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                             {"schedule_owner": "systemd"})


def test_manifest_loads_dormant():
    """The plugin discovers, resolves every entrypoint, and stays off
    by default (a new default-enabled plugin breaks the substrate's
    dormant baseline)."""
    from tools.dashboard.plugin_api import loader
    plugins = [p for p in loader.discover() if p.manifest.id == "backup"]
    assert len(plugins) == 1
    assert plugins[0].manifest.default_enabled is False
    loaded = [p for p in loader.load_all() if p.manifest.id == "backup"]
    assert len(loaded) == 1
    assert {r.path for r in loaded[0].routes} == {
        "/api/backup/summary", "/api/backup/runs",
        "/api/backup/drills", "/api/backup/config"}
