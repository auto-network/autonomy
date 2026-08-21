"""Acceptance tests for organization-scoped Agent Test history and UI."""
from __future__ import annotations

from pathlib import Path

import yaml

from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.testing.entrypoints import store
from tools.dashboard.plugins.testing.entrypoints.schemas import (
    ERROR_SET_ID,
    OBSERVATION_SET_ID,
    RUN_SET_ID,
    SCHEMA_REVISION,
    USAGE_SET_ID,
)
from tools.graph import org_ops, settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas import get_schema


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "testing"


def _run(repository: str, *, session: str = "auto-test", status: str = "passed") -> dict:
    return {
        "repository": repository,
        "session": session,
        "status": status,
        "mode": "run",
        "duration_seconds": 2.5,
        "created_at": "2026-08-21T00:00:00+00:00",
        "finished_at": "2026-08-21T00:00:03+00:00",
        "selectors": ["tests/test_widget.py"],
        "collected": 1,
        "passed": 1 if status == "passed" else 0,
        "failed": 1 if status == "failed" else 0,
        "errors": 0,
        "skipped": 0,
        "new_failures": 0,
        "known_failures": 0,
        "quarantined_failures": 0,
        "parallelism": 1,
        "agent_test_version": "0.4.0",
        "fingerprint": "abc",
        "rerun_of": "",
        "estimated_seconds": 2.0,
        "estimated_low_seconds": 1.5,
        "estimated_high_seconds": 3.0,
        "estimate_complete": True,
        "estimate_sampled_tests": 1,
    }


def _orgs(tmp_path, monkeypatch) -> None:
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.close_all_pooled()
    for slug in ("alpha", "beta"):
        org_ops.create_org(
            slug,
            type_="shared",
            identity_payload={"name": slug.title()},
            root=root,
        )
    monkeypatch.setattr(
        store.agent_test_leases,
        "activity_snapshot",
        lambda _org: {
            "ok": True,
            "limits": {"tests": 16, "browsers": 4},
            "used": {"tests": 0, "browsers": 0},
            "available": {"tests": 16, "browsers": 4},
            "active_leases": 0,
            "queued_requests": 0,
            "queued": {"tests": 0, "browsers": 0},
            "running": [],
            "queue": [],
            "global_active_leases": 0,
            "global_queued_requests": 0,
            "as_of": 1_800_000_000.0,
        },
    )
    monkeypatch.setattr(
        store.dashboard_db,
        "get_session",
        lambda session: {"label": f"Title for {session}"},
    )


def test_manifest_links_testing_ui_to_agent_test_capability() -> None:
    manifest = PluginManifest.model_validate(
        yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    )
    assert manifest.id == "testing"
    assert manifest.default_enabled is True
    assert manifest.capability is not None
    assert manifest.capability.contract == "test_execution"
    assert manifest.capability.implementation == "autonomy/agent-test"
    assert manifest.paths == ["/testing"]
    declarations = {row.set_id: row for row in manifest.settings}
    assert set(declarations) == {
        "autonomy.capability.contract",
        "autonomy.capability.impl",
        "autonomy.org.capability.install",
    }

    import json
    capability_root = Path(__file__).resolve().parents[3] / "agents" / "capabilities" / "agent_test"
    assert json.loads((PLUGIN_DIR / "settings/capability_contract.json").read_text()) == json.loads(
        (capability_root / "contract.json").read_text()
    )
    assert json.loads((PLUGIN_DIR / "settings/capability_impl.json").read_text()) == json.loads(
        (capability_root / "manifest.json").read_text()
    )
    install = json.loads((PLUGIN_DIR / "settings/capability_install.json").read_text())
    implementation = json.loads((PLUGIN_DIR / "settings/capability_impl.json").read_text())
    assert install["implementation"] == implementation["name"]
    assert install["implementation_version"] == implementation["version"]


def test_ui_has_pinned_org_picker_and_bounded_evidence_surfaces() -> None:
    html = (PLUGIN_DIR / "page.html").read_text()
    css = (PLUGIN_DIR / "page.css").read_text()
    script = (PLUGIN_DIR / "page.js").read_text()
    assert 'data-testid="testing-org-picker"' in html
    assert "position: sticky" in css
    assert 'data-testid="testing-live-activity"' in html
    assert 'data-testid="testing-session-queue"' in html
    assert 'data-testid="testing-feedback-loop"' in html
    assert 'data-testid="testing-operational-errors"' in html
    assert 'data-testid="testing-behavior-patterns"' in html
    assert 'data-testid="testing-estimate-quality"' in html
    assert "etaText(run)" in html
    assert "etaProgress(run)" in script
    assert "summary.activity.active_runs" in html
    assert "summary.activity.queued_runs" in html
    assert "summary.tests.observation_rows" in html
    assert "summary.tests.average_samples_per_test" in html
    assert "session.runs.slice(0, 5)" in html
    assert "+' + (session.runs.length - 5)" in html
    assert "@media (max-width: 719px)" in css
    assert "overflow-x: hidden" in css
    assert ".testing-console * { min-width: 0; }" in css
    assert "min-height: 44px" in css
    assert "setInterval" in script
    assert "5000" in script
    assert "recent_limit: '20'" in script
    assert "ranked_limit: '10'" in script
    assert "X-Graph-Org" in script
    assert "left === this.selectedOrg" in script
    assert "/api/plugins/testing/summary" in script


def test_history_is_raw_org_owned_append_only_capped_and_isolated(tmp_path, monkeypatch) -> None:
    _orgs(tmp_path, monkeypatch)
    run_schema = get_schema(RUN_SET_ID, SCHEMA_REVISION)
    observation_schema = get_schema(OBSERVATION_SET_ID, SCHEMA_REVISION)
    usage_schema = get_schema(USAGE_SET_ID, SCHEMA_REVISION)
    error_schema = get_schema(ERROR_SET_ID, SCHEMA_REVISION)
    assert run_schema._home == "organization"
    assert run_schema._publication_band == ("raw", "raw")
    assert observation_schema._home == "organization"
    assert observation_schema._publication_band == ("raw", "raw")
    assert observation_schema._access_pattern == "append_only_log"
    assert usage_schema._home == "organization"
    assert usage_schema._publication_band == ("raw", "raw")
    assert usage_schema._access_pattern == "append_only_log"
    assert error_schema._home == "organization"
    assert error_schema._publication_band == ("raw", "raw")
    assert error_schema._access_pattern == "append_only_log"

    nodeid = "tests/test_widget.py::test_widget"
    clock = [1_800_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    for index in range(12):
        run_id = f"alpha-{index}"
        assert store.record_run("alpha", run_id, _run("github.test/acme/widget"))["ok"]
        clock[0] += 1
        result = store.record_observations(
            "alpha", "github.test/acme/widget", run_id,
            [{"nodeid": nodeid, "duration_seconds": float(index), "outcome": "failed" if index == 5 else "passed"}],
        )
        assert result["ok"]
    assert store.record_run("beta", "beta-1", _run("github.test/acme/secret"))["ok"]

    alpha = settings_ops.read_owned_set(OBSERVATION_SET_ID, org="alpha").members
    beta_runs = settings_ops.read_owned_set(RUN_SET_ID, org="beta").members
    assert len(alpha) == 10
    assert {member.payload["run_id"] for member in alpha} == {f"alpha-{i}" for i in range(2, 12)}
    assert [member.key for member in beta_runs] == ["beta-1"]
    assert "github.test/acme/secret" not in store.dashboard_summary("alpha")["repositories"]
    assert "github.test/acme/widget" not in store.dashboard_summary("beta")["repositories"]

    duplicate = store.record_observations(
        "alpha", "github.test/acme/widget", "alpha-11",
        [{"nodeid": nodeid, "duration_seconds": 99.0, "outcome": "failed"}],
    )
    assert duplicate["appended"] == 0
    assert duplicate["duplicates"] == 1

    history = store.duration_history("alpha", "github.test/acme/widget", [nodeid])
    assert len(history["tests"][0]["observations"]) == 10
    assert history["tests"][0]["median_seconds"] == 6.5
    assert history["tests"][0]["minimum_seconds"] == 2.0
    assert history["tests"][0]["maximum_seconds"] == 11.0
    estimate = store.estimate_duration("alpha", "github.test/acme/widget", [nodeid], parallelism=2)
    assert estimate["estimated_seconds"] == 6.5
    assert estimate["estimated_low_seconds"] == 2.0
    assert estimate["estimated_high_seconds"] == 11.0
    assert estimate["parallelism"] == 2
    assert estimate["effective_parallelism"] == 1
    assert estimate["selector_history_coverage"] == 1.0
    assert estimate["estimate_complete"] is True

    partial = store.estimate_duration(
        "alpha", "github.test/acme/widget", [nodeid, "tests/test_unseen.py"], parallelism=2,
    )
    assert partial["estimated_seconds"] == 6.5
    assert partial["unknown_selectors"] == ["tests/test_unseen.py"]
    assert partial["selector_history_coverage"] == 0.5
    assert partial["estimate_complete"] is False

    broad = store.estimate_duration(
        "alpha", "github.test/acme/widget", ["tests/test_widget.py"], parallelism=1,
    )
    assert broad["unknown_selectors"] == []
    assert broad["open_ended_selectors"] == ["tests/test_widget.py"]
    assert broad["estimate_complete"] is False

    summary = store.dashboard_summary("alpha", repository="github.test/acme/widget")
    assert summary["tests"]["observation_rows"] == 10
    assert summary["tests"]["maximum_rows_for_observed_tests"] == 10
    assert summary["tests"]["average_samples_per_test"] == 10.0
    assert summary["tests"]["sample_depths"] == {"10": 1}
    assert summary["retention"] == {
        "timing_observations_per_test": 10,
        "terminal_runs_per_repository": 2_000,
        "usage_events_per_organization": 5_000,
        "operational_errors_per_organization": 2_000,
    }


def test_summary_reports_pass_ratio_flakes_slow_tests_and_telemetry(tmp_path, monkeypatch) -> None:
    _orgs(tmp_path, monkeypatch)
    repository = "github.test/acme/widget"
    for run_id, status, outcome, duration in (
        ("run-pass", "passed", "passed", 1.0),
        ("run-fail", "failed", "failed", 9.0),
    ):
        assert store.record_run("alpha", run_id, _run(repository, status=status))["ok"]
        assert store.record_observations(
            "alpha", repository, run_id,
            [{"nodeid": "tests/test_widget.py::test_widget", "duration_seconds": duration, "outcome": outcome}],
        )["ok"]
    store.record_event("alpha", "auto-test", "run_started")
    monkeypatch.setattr(store.time, "time", lambda: 1_777_000_010.0)
    monkeypatch.setattr(
        store.agent_test_leases,
        "activity_snapshot",
        lambda _org: {
            "ok": True,
            "limits": {"tests": 16, "browsers": 4},
            "used": {"tests": 4, "browsers": 0},
            "available": {"tests": 12, "browsers": 4},
            "active_leases": 1,
            "queued_requests": 1,
            "queued": {"tests": 2, "browsers": 0},
            "running": [{
                "session": "auto-live", "run_id": "run-live",
                "repository": repository, "selectors": ["tests/test_widget.py"],
                "selector_count": 1, "resources": {"tests": 4},
                "acquired_at": 1_777_000_000.0,
            }],
            "queue": [{
                "session": "auto-wait", "run_id": "run-wait",
                "repository": repository, "selectors": ["tests/test_other.py"],
                "selector_count": 1, "resources": {"tests": 2},
                "requested_at": 1_777_000_005.0,
            }],
            "global_active_leases": 1,
            "global_queued_requests": 1,
            "as_of": 1_777_000_010.0,
        },
    )
    summary = store.dashboard_summary("alpha", repository, recent_limit=1)
    assert summary["organization"] == "alpha"
    assert summary["runs"]["total"] == 2
    assert summary["runs"]["pass_ratio"] == 0.5
    assert summary["runs"]["estimate_quality"]["samples"] == 2
    assert summary["runs"]["estimate_quality"]["within_factor_2"] == 1.0
    assert summary["tests"]["flaky"][0]["nodeid"].endswith("::test_widget")
    assert summary["tests"]["slow"][0]["median_seconds"] == 5.0
    assert summary["telemetry"]["counts"] == {"run_started": 1}
    assert summary["activity"]["active_runs"] == 1
    assert summary["activity"]["queued_runs"] == 1
    assert summary["activity"]["recent_runs"] == 2
    assert summary["activity"]["recent_tests"] == 2
    assert len(summary["activity"]["just_finished"]) == 1
    assert summary["activity"]["unresolved_starts"] == 1
    assert summary["activity"]["sessions"][0]["session_title"] == "Title for auto-live"
    assert summary["activity"]["sessions"][1]["session_title"] == "Title for auto-wait"
    assert "selectors" not in summary["recent_runs"][0]
    assert summary["recent_runs"][0]["selector_count"] == 1


def test_usage_and_operational_errors_are_capped_org_local_feedback(tmp_path, monkeypatch) -> None:
    _orgs(tmp_path, monkeypatch)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    monkeypatch.setattr(store, "MAX_USAGE_EVENTS", 2)
    monkeypatch.setattr(store, "MAX_ERROR_EVENTS", 2)

    for index, event in enumerate(("command_plan", "command_run_changed", "repeat_refused")):
        clock[0] += 1
        assert store.record_event("alpha", "auto-alpha", event, "0.5.0")["ok"]
    store.record_event("beta", "auto-beta", "command_status", "9.9.9")
    assert len(settings_ops.read_owned_set(USAGE_SET_ID, org="alpha").members) == 2
    assert len(settings_ops.read_owned_set(USAGE_SET_ID, org="beta").members) == 1
    telemetry = store.telemetry_status("alpha")
    assert telemetry["feature_counts"]["command_plan"] == 1
    assert telemetry["versions"] == {"0.5.0": 1}
    assert {event["event"] for event in telemetry["recent_events"]} == {
        "command_run_changed", "repeat_refused",
    }

    for index, category in enumerate(("coordinator_unavailable", "lease_renewal", "process_start")):
        clock[0] += 1
        assert store.record_error(
            "alpha",
            "auto-alpha",
            run_id=f"at-{index}",
            phase="execution",
            category=category,
            message=f"bounded diagnostic {index}",
            agent_test_version="0.5.0",
        )["ok"]
    errors = store.error_status("alpha")
    assert errors["total"] == 2
    assert errors["counts"] == {"lease_renewal": 1, "process_start": 1}
    assert store.error_status("beta")["total"] == 0
    summary = store.dashboard_summary("alpha")
    assert summary["operational_errors"]["recent"][0]["session_title"] == "Title for auto-alpha"


def test_usage_sequences_expose_effective_and_problematic_agent_patterns(tmp_path, monkeypatch) -> None:
    _orgs(tmp_path, monkeypatch)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    events = (
        "command_run_explicit", "run_started", "command_status", "command_status",
        "run_failed", "command_failures", "command_output", "command_rerun_failures",
        "run_started", "run_passed", "command_retain", "raw_pytest_refused",
        "repeat_refused", "capacity_queued",
    )
    for event in events:
        clock[0] += 10
        assert store.record_event("alpha", "auto-pattern", event, "0.5.1")["ok"]

    behavior = store.telemetry_status("alpha")["behavior"]
    assert behavior["session_count"] == 1
    assert behavior["totals"] == {
        "completed_runs": 2,
        "background_runs": 1,
        "polled_runs": 1,
        "status_polls": 2,
        "status_poll_bursts": 1,
        "repeated_commands": 1,
        "failed_runs_inspected": 1,
        "focused_recoveries": 1,
        "broad_recovery_attempts": 0,
        "repeat_refusals": 1,
        "raw_pytest_refusals": 1,
        "queue_waits": 1,
        "output_reads": 1,
        "retained_runs": 1,
    }
    row = behavior["sessions"][0]
    assert row["session_title"] == "Title for auto-pattern"
    assert row["agent_test_version"] == "0.5.1"
    assert len(row["recent_sequence"]) == 10
