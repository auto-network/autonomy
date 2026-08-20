from __future__ import annotations

import json
import http.server
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .test_stage1 import _cli, _manifests, _wait_terminal, cli_env, project  # noqa: F401
from tools.agent_test.environment import pytest_parallelism, requested_resources


def test_doctor_prefers_project_venv_without_shell_activation(project: Path, cli_env: dict[str, str]):
    interpreter = project / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(Path(sys.executable).resolve())

    result = _cli(project, cli_env, "doctor")

    assert result.returncode == 0
    assert str(interpreter) in result.stdout
    assert "shell activation is not required" in result.stdout


def test_resource_weight_infers_xdist_and_profile_browser_slots(project: Path):
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--numprocesses=6"\n'
    )

    resources = requested_resources(project, {"resources": {"browsers": 2}}, "run")

    assert resources == {"tests": 6, "browsers": 2}
    assert pytest_parallelism(project, {"resources": {"tests": 12}}) == 6
    assert requested_resources(project, {"resources": {"tests": 12}}, "run") == {"tests": 12}


def test_unchanged_rerun_is_refused_but_failure_only_rerun_is_allowed(project: Path, cli_env: dict[str, str]):
    (project / "test_mix.py").write_text(
        "def test_pass():\n    pass\n\n"
        "def test_fail():\n    assert False, 'retained failure'\n"
    )
    first = _cli(project, cli_env, "run", "test_mix.py")
    assert first.returncode == 0
    original = _wait_terminal(cli_env)
    assert original["summary"]["new_failures"] == 1

    refused = _cli(project, cli_env, "run", "test_mix.py")
    assert refused.returncode == 4
    assert original["run_id"] in refused.stderr
    assert len(_manifests(cli_env)) == 1

    rerun = _cli(project, cli_env, "rerun-failures", original["run_id"])
    assert rerun.returncode == 0, rerun.stderr
    repeated = _wait_terminal(cli_env)
    assert repeated["run_id"] != original["run_id"]
    assert repeated["selectors"] == ["test_mix.py::test_fail"]
    assert repeated["rerun_of"] == original["run_id"]
    assert repeated["summary"]["known_failures"] == 1


def test_collection_inventory_and_profiles_are_bounded(project: Path, cli_env: dict[str, str]):
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = \"\"\n"
        "[tool.agent-test.profiles.smoke]\nselectors = [\"test_inventory.py\"]\n"
    )
    (project / "test_inventory.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('value', range(25))\n"
        "def test_value(value):\n    assert value >= 0\n"
    )

    listed = _cli(project, cli_env, "profiles")
    assert "smoke: 1 selector" in listed.stdout
    started = _cli(project, cli_env, "collect", "--profile", "smoke")
    assert started.returncode == 0, started.stderr
    collected = _wait_terminal(cli_env)
    assert collected["status"] == "collected"
    assert collected["summary"]["collected"] == 25
    assert collected["resources"] == {"tests": 1}

    inventory = _cli(project, cli_env, "inventory", collected["run_id"], "--limit", "5")
    assert inventory.returncode == 0
    assert "5 of 25 retained test node" in inventory.stdout
    assert "20 more retained" in inventory.stdout
    assert len(inventory.stdout.splitlines()) <= 7
    valid = _cli(project, cli_env, "validate", "test_inventory.py::test_value[3]", "--run", collected["run_id"])
    assert valid.returncode == 0
    invalid = _cli(project, cli_env, "validate", "test_inventory.py::test_absent", "--run", collected["run_id"])
    assert invalid.returncode == 2
    assert "do not exist" in invalid.stderr


def test_quarantine_classification_is_retained(project: Path, cli_env: dict[str, str]):
    (project / "tests").mkdir()
    (project / "tests/quarantine_baseline_20260820.txt").write_text("test_q.py::test_quarantined\n")
    (project / "test_q.py").write_text("def test_quarantined():\n    assert False\n")

    started = _cli(project, cli_env, "run", "test_q.py")
    assert started.returncode == 0
    final = _wait_terminal(cli_env)
    failures = json.loads((Path(final["failures_path"])).read_text())
    assert failures[0]["classification"] == "quarantined"
    assert final["summary"]["quarantined_failures"] == 1


def test_resident_supervisor_is_reused_across_changed_runs(project: Path, cli_env: dict[str, str]):
    env = dict(cli_env)
    env.pop("AGENT_TEST_NO_SUPERVISOR")
    (project / "test_live.py").write_text("def test_live():\n    pass\n")
    first = _cli(project, env, "run", "test_live.py")
    assert first.returncode == 0, first.stderr
    one = _wait_terminal(env)
    supervisor_pid = one["supervisor_pid"]

    (project / "test_live.py").write_text("def test_live():\n    assert 1 == 1\n")
    second = _cli(project, env, "run", "test_live.py")
    assert second.returncode == 0, second.stderr
    two = _wait_terminal(env)
    assert two["supervisor_pid"] == supervisor_pid
    status = _cli(project, env, "supervisor")
    assert f"pid {supervisor_pid}" in status.stdout
    os.kill(supervisor_pid, signal.SIGTERM)


def test_worker_waits_for_machine_lease_before_starting_pytest(project: Path, cli_env: dict[str, str]):
    actions: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            action = body["action"]
            actions.append(action)
            if action == "acquire" and actions.count("acquire") == 1:
                response = {"ok": True, "state": "queued", "unavailable": {"tests": {"used": 16, "limit": 16}}}
            elif action == "acquire":
                response = {"ok": True, "state": "granted", "expires_at": time.time() + 90}
            elif action == "release":
                response = {"ok": True, "state": "released"}
            else:
                response = {"ok": True, "state": "granted", "expires_at": time.time() + 90}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(cli_env)
    env["AUTONOMY_SESSION"] = "auto-lease-test"
    env["AGENT_TEST_MACHINE_LEASE"] = "auto"
    env["AGENT_TEST_DASHBOARD"] = f"http://127.0.0.1:{server.server_port}"
    marker = project / "pytest-started"
    (project / "test_lease.py").write_text(
        f"from pathlib import Path\n\ndef test_lease():\n    Path({str(marker)!r}).write_text('yes')\n"
    )
    try:
        started = _cli(project, env, "run", "test_lease.py")
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 1
        queued = None
        while time.monotonic() < deadline:
            queued = _manifests(env)[0]
            if queued["status"] == "queued":
                break
            time.sleep(0.02)
        assert queued["status"] == "queued"
        assert not marker.exists()
        final = _wait_terminal(env)
        assert final["status"] == "passed"
        assert final["machine_lease"]["state"] == "released"
        assert marker.exists()
        lease_actions = [action for action in actions if action in {"acquire", "renew", "release"}]
        assert lease_actions[:2] == ["acquire", "acquire"]
        assert lease_actions[-1] == "release"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
