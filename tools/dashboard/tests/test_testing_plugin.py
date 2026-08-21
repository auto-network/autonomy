"""Acceptance tests for organization-scoped Agent Test history and UI."""
from __future__ import annotations

from pathlib import Path

import yaml

from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.testing.entrypoints import store
from tools.dashboard.plugins.testing.entrypoints.schemas import (
    OBSERVATION_SET_ID,
    RUN_SET_ID,
    SCHEMA_REVISION,
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


def test_ui_has_pinned_org_picker_and_bounded_evidence_surfaces() -> None:
    html = (PLUGIN_DIR / "page.html").read_text()
    css = (PLUGIN_DIR / "page.css").read_text()
    script = (PLUGIN_DIR / "page.js").read_text()
    assert 'data-testid="testing-org-picker"' in html
    assert "position: sticky" in css
    assert "recent_limit: '20'" in script
    assert "ranked_limit: '10'" in script
    assert "X-Graph-Org" in script
    assert "/api/plugins/testing/summary" in script


def test_history_is_raw_org_owned_append_only_capped_and_isolated(tmp_path, monkeypatch) -> None:
    _orgs(tmp_path, monkeypatch)
    run_schema = get_schema(RUN_SET_ID, SCHEMA_REVISION)
    observation_schema = get_schema(OBSERVATION_SET_ID, SCHEMA_REVISION)
    assert run_schema._home == "organization"
    assert run_schema._publication_band == ("raw", "raw")
    assert observation_schema._home == "organization"
    assert observation_schema._publication_band == ("raw", "raw")
    assert observation_schema._access_pattern == "append_only_log"

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
    estimate = store.estimate_duration("alpha", "github.test/acme/widget", [nodeid], parallelism=2)
    assert estimate["estimated_seconds"] == 3.25


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
    summary = store.dashboard_summary("alpha", repository)
    assert summary["organization"] == "alpha"
    assert summary["runs"]["total"] == 2
    assert summary["runs"]["pass_ratio"] == 0.5
    assert summary["tests"]["flaky"][0]["nodeid"].endswith("::test_widget")
    assert summary["tests"]["slow"][0]["median_seconds"] == 5.0
    assert summary["telemetry"]["counts"] == {"run_started": 1}
    assert "selectors" not in summary["recent_runs"][0]
    assert summary["recent_runs"][0]["selector_count"] == 1
