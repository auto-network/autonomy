from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from .test_stage1 import _cli, _wait_terminal, cli_env, project  # noqa: F401


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )


def test_changed_plan_runs_named_test_and_reports_changed_line_coverage(
    project: Path, cli_env: dict[str, str],
):
    (project / "calc.py").write_text(
        "def classify(value):\n    return 'old'\n"
    )
    (project / "tests").mkdir()
    (project / "tests/test_calc.py").write_text(
        "from calc import classify\n\ndef test_positive():\n    assert classify(2) == 'positive'\n"
    )
    _git(project, "init", "-q")
    _git(project, "add", ".")
    _git(
        project,
        "-c", "user.name=Agent Test",
        "-c", "user.email=agent-test@example.invalid",
        "commit", "-qm", "baseline",
    )
    (project / "calc.py").write_text(
        "def classify(value):\n"
        "    if value > 0:\n"
        "        return 'positive'\n"
        "    return 'other'\n"
    )

    plan = _cli(project, cli_env, "plan")
    assert plan.returncode == 0, plan.stderr
    assert "tests/test_calc.py" in plan.stdout
    started = _cli(project, cli_env, "run", "--changed")
    assert started.returncode == 0, started.stderr
    final = _wait_terminal(cli_env)
    assert final["status"] == "passed"
    assert final["selectors"] == ["tests/test_calc.py"]

    coverage = _cli(project, cli_env, "coverage", final["run_id"])
    assert coverage.returncode == 0
    assert "changed-line coverage 2/3 (66.7%)" in coverage.stdout
    assert "calc.py: 2/3" in coverage.stdout
    assert "missing lines 4" in coverage.stdout


def test_baseline_publishes_retained_failures(project: Path, cli_env: dict[str, str]):
    (project / "test_failure.py").write_text("def test_failure():\n    assert False\n")
    started = _cli(project, cli_env, "run", "test_failure.py")
    assert started.returncode == 0
    failed = _wait_terminal(cli_env)
    assert failed["status"] == "failed"

    published = _cli(project, cli_env, "baseline", failed["run_id"])

    assert published.returncode == 0
    baseline = json.loads((Path(cli_env["AGENT_TEST_STATE_DIR"]) / "baseline.json").read_text())
    assert baseline["run_id"] == failed["run_id"]
    assert baseline["failures"] == ["test_failure.py::test_failure"]


def test_repository_hook_refuses_python_module_pytest_for_agents(monkeypatch):
    import conftest as root_conftest
    from tools.agent_test import lease_client

    events: list[str] = []

    class Refused(RuntimeError):
        pass

    monkeypatch.setenv("AUTONOMY_SESSION", "auto-refusal-test")
    monkeypatch.delenv("AGENT_TEST_INTERNAL", raising=False)
    monkeypatch.delenv("PYTEST_ALLOW_RAW", raising=False)
    monkeypatch.setattr(lease_client, "telemetry_request", lambda *, event: events.append(event))
    monkeypatch.setattr(
        root_conftest.pytest,
        "exit",
        lambda message, returncode: (_ for _ in ()).throw(Refused(f"{returncode}:{message}")),
    )

    with pytest.raises(Refused, match="64:Raw pytest is disabled"):
        root_conftest.pytest_configure(None)
    assert events == ["raw_pytest_refused"]


def test_capability_primer_teaches_agent_test_not_raw_pytest():
    primer = Path(__file__).resolve().parents[3] / "agents/capabilities/agent_test/primer.md"
    text = primer.read_text()

    assert "agent-test run --changed" in text
    assert "only supported Python test entry point" in text
    assert "python3 -m pytest" not in text
    assert "ALWAYS pipe test output through `tee`" not in text


def test_dashboard_notification_carries_session_bearer(monkeypatch):
    from tools.agent_test import worker

    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, **_kwargs):
        captured["authorization"] = request.headers.get("Authorization")
        return Response()

    monkeypatch.setenv("CROSSTALK_TOKEN", "test-session-bearer")
    monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
    delivered, _detail = worker._dashboard_notify("auto-test", "at-1", "passed", "done")

    assert delivered is True
    assert captured["authorization"] == "Bearer test-session-bearer"
