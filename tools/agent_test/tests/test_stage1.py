from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


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


def _wait_terminal(env: dict[str, str], timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        values = _manifests(env)
        if values and values[0]["status"] not in {"starting", "running", "stopping"}:
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

    second = _cli(project, cli_env, "run", "test_ok.py")
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
