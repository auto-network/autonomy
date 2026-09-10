"""Where the harness finds its dashboard, and which past runs block a rerun.

Three runs in one compose session (2026-09-10) recorded "machine capacity
coordinator unavailable" because the client dialled localhost:8080, where a
compose session has nothing listening, while GRAPH_API named the dashboard
the whole time. Exporting the override after the first run did not help
either: the resident supervisor had copied its environment at start and
every worker inherited that copy. And the errored runs then tripped the
unchanged-run guard, refusing the retry that would have worked. These pin
all three halves.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from tools.agent_test import lease_client, supervisor
from tools.agent_test.store import (
    atomic_write_json,
    manifest_path,
    previous_verdict,
    run_dir,
)


def _clear(monkeypatch):
    for name in ("AGENT_TEST_DASHBOARD", "GRAPH_API", "AUTONOMY_SESSION"):
        monkeypatch.delenv(name, raising=False)


def test_explicit_override_wins_over_the_session_address(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GRAPH_API", "https://dashboard:8080")
    monkeypatch.setenv("AGENT_TEST_DASHBOARD", "http://127.0.0.1:9999/")
    assert lease_client.dashboard_base() == "http://127.0.0.1:9999"


def test_session_address_is_used_when_no_override_is_set(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GRAPH_API", "https://dashboard:8080/")
    assert lease_client.dashboard_base() == "https://dashboard:8080"


def test_blank_variables_fall_through_to_localhost(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("AGENT_TEST_DASHBOARD", "  ")
    monkeypatch.setenv("GRAPH_API", "")
    assert lease_client.dashboard_base() == lease_client.DEFAULT_DASHBOARD


def test_the_session_address_alone_configures_a_coordinator(monkeypatch):
    _clear(monkeypatch)
    assert lease_client.coordinator_configured() is False
    monkeypatch.setenv("GRAPH_API", "https://dashboard:8080")
    assert lease_client.coordinator_configured() is True


def test_history_requests_dial_the_session_address(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GRAPH_API", "https://dashboard:8080")
    seen = []

    def refuse(request, timeout=None, context=None):
        seen.append(request.full_url)
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    result = lease_client.duration_request("estimate")
    assert result["unavailable"] is True
    assert seen == ["https://dashboard:8080/api/plugins/testing/durations"]


def test_worker_takes_coordination_env_from_the_requesting_cli():
    requested = supervisor.worker_env_overrides({
        "AGENT_TEST_DASHBOARD": "https://dashboard:8080",
        "CROSSTALK_TOKEN": "tok-new",
        "PATH": "/evil",
    })
    assert requested == {
        "AGENT_TEST_DASHBOARD": "https://dashboard:8080",
        "CROSSTALK_TOKEN": "tok-new",
    }
    stale = {"PATH": "/usr/bin", "CROSSTALK_TOKEN": "tok-old", "GRAPH_API": "https://x"}
    env = supervisor.worker_env({**requested, "PATH": "/evil", "GRAPH_API": 7}, stale)
    assert env == {
        "PATH": "/usr/bin",                       # not passthrough: untouched
        "CROSSTALK_TOKEN": "tok-new",             # replaced by the CLI's
        "GRAPH_API": "https://x",                 # non-string override ignored
        "AGENT_TEST_DASHBOARD": "https://dashboard:8080",
    }
    assert supervisor.worker_env(None, stale) == stale
    assert supervisor.worker_env("junk", stale) == stale


def _manifest(root, run_id, status, fingerprint, created_at):
    directory = run_dir(root, run_id)
    directory.mkdir(parents=True)
    atomic_write_json(manifest_path(directory), {
        "schema": 1, "run_id": run_id, "status": status,
        "fingerprint": fingerprint, "created_at": created_at,
        "finished_at": created_at, "selectors": ["t.py"],
    })


def test_only_a_run_that_judged_the_code_blocks_an_unchanged_rerun(tmp_path):
    root = tmp_path / "state"
    _manifest(root, "at-1", "error", "fp", "2026-09-10T01:41:39Z")
    _manifest(root, "at-2", "stopped", "fp", "2026-09-10T01:42:39Z")
    assert previous_verdict(root, "fp") is None

    _manifest(root, "at-3", "failed", "fp", "2026-09-10T01:43:39Z")
    assert previous_verdict(root, "fp")["run_id"] == "at-3"
    assert previous_verdict(root, "other") is None
