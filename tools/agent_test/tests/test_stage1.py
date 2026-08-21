from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.agent_test.store import state_root


REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = ""\n')
    return root


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if part
    )
    env["AGENT_TEST_STATE_DIR"] = str(tmp_path / "state")
    env["AGENT_TEST_NOTIFY"] = "none"
    env["AGENT_TEST_NO_SUPERVISOR"] = "1"
    env["AGENT_TEST_MACHINE_LEASE"] = "none"
    env.pop("AUTONOMY_SESSION", None)
    env.pop("CROSSTALK_TOKEN", None)
    return env


def _cli(project: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tools.agent_test", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _manifests(env: dict[str, str]) -> list[dict]:
    paths = Path(env["AGENT_TEST_STATE_DIR"]).glob("runs/*/run.json")
    values = [json.loads(path.read_text()) for path in paths]
    return sorted(values, key=lambda value: value["created_at"], reverse=True)


def _wait_terminal(env: dict[str, str], timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        values = _manifests(env)
        if values and values[0]["status"] not in {"starting", "queued", "running", "stopping"}:
            return values[0]
        time.sleep(0.05)
    raise AssertionError("Agent Test run did not reach a terminal state")


def test_run_returns_immediately_and_retains_passing_result(project: Path, cli_env: dict[str, str]):
    (project / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(0.8)\n    print('ERROR is application noise')\n"
    )
    started = time.monotonic()
    result = _cli(project, cli_env, "run", "test_slow.py")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 0.7
    assert "Keep working; do not poll" in result.stdout
    final = _wait_terminal(cli_env)
    assert final["status"] == "passed"
    assert final["summary"]["passed"] == 1
    assert "ERROR is application noise" in Path(final["pytest_log"]).read_text()


def test_failure_queries_are_bounded_and_do_not_execute(project: Path, cli_env: dict[str, str]):
    (project / "test_many.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('value', range(7))\n"
        "def test_failure(value):\n    assert value < 0, f'bad value {value}'\n"
    )
    started = _cli(project, cli_env, "run", "test_many.py")
    assert started.returncode == 0
    final = _wait_terminal(cli_env)
    assert final["status"] == "failed"
    run_id = final["run_id"]
    before = len(_manifests(cli_env))

    failures = _cli(project, cli_env, "failures", run_id)
    assert failures.returncode == 0
    assert "5 of 7 failures" in failures.stdout
    assert "2 more retained" in failures.stdout
    assert len(_manifests(cli_env)) == before

    trace = _cli(project, cli_env, "trace", run_id, "1", "--limit-lines", "20")
    assert trace.returncode == 0
    assert "assert value < 0" in trace.stdout
    assert len(_manifests(cli_env)) == before


def test_second_live_run_is_refused_and_stop_owns_process_group(project: Path, cli_env: dict[str, str]):
    (project / "test_wait.py").write_text(
        "import time\n\ndef test_wait():\n    time.sleep(5)\n"
    )
    first = _cli(project, cli_env, "run", "test_wait.py")
    assert first.returncode == 0
    second = _cli(project, cli_env, "run", "test_wait.py")
    assert second.returncode == 3
    assert "second run was not started" in second.stderr
    assert len(_manifests(cli_env)) == 1

    stopped = _cli(project, cli_env, "stop")
    assert stopped.returncode == 0
    final = _wait_terminal(cli_env)
    assert final["status"] == "stopped"


def test_guidance_becomes_compact_after_first_completed_run(project: Path, cli_env: dict[str, str]):
    (project / "test_ok.py").write_text("def test_ok():\n    pass\n")
    first = _cli(project, cli_env, "run", "test_ok.py")
    assert "Keep working; do not poll" in first.stdout
    _wait_terminal(cli_env)

    (project / "test_other.py").write_text("def test_other():\n    pass\n")
    second = _cli(project, cli_env, "run", "test_other.py")
    assert second.returncode == 0
    assert "Completion will be delivered here" in second.stdout
    assert "Keep working; do not poll" not in second.stdout
    _wait_terminal(cli_env)


def test_output_query_has_a_defaultable_hard_limit(project: Path, cli_env: dict[str, str]):
    (project / "test_lines.py").write_text(
        "def test_lines():\n"
        "    for value in range(30):\n"
        "        print(f'line-{value}')\n"
    )
    started = _cli(project, cli_env, "run", "test_lines.py")
    assert started.returncode == 0, started.stderr
    final = _wait_terminal(cli_env)
    output = _cli(
        project,
        cli_env,
        "output",
        final["run_id"],
        "--limit-lines",
        "5",
    )
    assert output.returncode == 0
    assert "line-29" in output.stdout
    assert "earlier lines retained" in output.stdout
    assert len(output.stdout.splitlines()) <= 6


def test_default_run_state_is_private_temporary_storage(project: Path, monkeypatch, tmp_path: Path):
    monkeypatch.delenv("AGENT_TEST_STATE_DIR", raising=False)
    monkeypatch.setenv("AGENT_TEST_TMPDIR", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_SESSION", "auto-private-test")
    root = state_root(project)
    assert root.is_relative_to(tmp_path / ".agent-test" / "auto-private-test")
    assert "/workspace/output" not in str(root)


def test_retain_explicitly_copies_completed_run_to_workspace_output(
    project: Path, cli_env: dict[str, str], tmp_path: Path,
):
    retain_root = tmp_path / "workspace-output" / "agent-test"
    cli_env["AGENT_TEST_RETAIN_DIR"] = str(retain_root)
    (project / "test_keep.py").write_text("def test_keep():\n    pass\n")
    started = _cli(project, cli_env, "run", "test_keep.py")
    assert started.returncode == 0, started.stderr
    final = _wait_terminal(cli_env)

    retained = _cli(project, cli_env, "retain", final["run_id"])
    assert retained.returncode == 0, retained.stderr
    destination = retain_root / Path(cli_env["AGENT_TEST_STATE_DIR"]).name / "runs" / final["run_id"]
    retained_manifest = json.loads((destination / "run.json").read_text())
    assert retained_manifest["evidence_lifecycle"] == "durable"
    assert retained_manifest["retained_at"]


def test_status_reports_live_eta_once_and_primes_against_polling(
    project: Path, cli_env: dict[str, str],
):
    directory = Path(cli_env["AGENT_TEST_STATE_DIR"]) / "runs" / "at-eta"
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "schema": 1,
        "run_id": "at-eta",
        "status": "running",
        "created_at": "2026-08-21T00:00:00+00:00",
        "started_at": "2026-08-21T00:00:00+00:00",
        "worker_pid": os.getpid(),
        "duration_estimate": {
            "estimated_seconds": 60.0,
            "estimated_low_seconds": 40.0,
            "estimated_high_seconds": 90.0,
            "unknown_selectors": [],
        },
    }))
    result = _cli(project, cli_env, "status")
    assert result.returncode == 0
    assert "ETA: original ~1.0m estimate exceeded" in result.stdout
    assert "Do not poll status again" in result.stdout


def test_status_does_not_claim_total_eta_when_a_selector_has_no_history(
    project: Path, cli_env: dict[str, str],
):
    directory = Path(cli_env["AGENT_TEST_STATE_DIR"]) / "runs" / "at-partial-eta"
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "schema": 1,
        "run_id": "at-partial-eta",
        "status": "queued",
        "created_at": "2026-08-21T00:00:00+00:00",
        "worker_pid": os.getpid(),
        "duration_estimate": {
            "estimated_seconds": 25.0,
            "estimated_low_seconds": 18.0,
            "estimated_high_seconds": 40.0,
            "unknown_selectors": ["tests/test_unseen.py"],
        },
    }))
    result = _cli(project, cli_env, "status")
    assert result.returncode == 0
    assert "ETA unknown: observed known work is at least ~18.0s" in result.stdout
    assert "1 selector(s) have no timing history" in result.stdout
    assert "Estimated runtime after admission" not in result.stdout
