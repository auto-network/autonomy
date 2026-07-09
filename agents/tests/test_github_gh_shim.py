"""Tests for the gh passthrough shim (auto-hof9c, design f7c4c109-91a §1c).

The shim's contract: byte-for-byte passthrough for everything, exit code
always the real gh's, and exactly one post-hook — declare-review-binding.sh
after a successful ``pr create``. Tests copy the shim into a tmp dir with a
stub helper beside it and a stub ``gh`` on PATH, then drive it like a
session would.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SHIM_SRC = Path(__file__).resolve().parents[1] / "capabilities" / "github" / "bin" / "gh"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def shim_env(tmp_path):
    """Shim + stub helper in one dir; stub gh in a separate PATH dir."""
    tool_dir = tmp_path / "github-tools"
    tool_dir.mkdir()
    shim = tool_dir / "gh"
    shutil.copy(SHIM_SRC, shim)
    shim.chmod(0o755)

    calls = tmp_path / "calls.log"
    helper_log = tmp_path / "helper.log"

    _write_exec(tool_dir / "declare-review-binding.sh", (
        "#!/bin/sh\n"
        f"echo helper-ran >> {helper_log}\n"
        "exit ${HELPER_RC:-0}\n"
    ))

    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    _write_exec(path_dir / "gh", (
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" >> {calls}\n'
        'echo "real-gh-stdout $*"\n'
        "exit ${GH_RC:-0}\n"
    ))

    env = {
        **os.environ,
        "PATH": f"{path_dir}:{os.environ['PATH']}",
        # Point the cap-bin exclusion somewhere inert.
        "AUTONOMY_CAPABILITY_BIN": str(tmp_path / "cap-bin"),
    }
    return shim, env, calls, helper_log


def _run(shim, env, *args, extra_env=None):
    return subprocess.run(
        [str(shim), *args],
        env={**env, **(extra_env or {})},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_non_pr_create_passes_through_verbatim(shim_env):
    shim, env, calls, helper_log = shim_env
    result = _run(shim, env, "api", "-i", "/repos/x/y/pulls/1",
                  "--header", "If-None-Match: abc")
    assert result.returncode == 0
    assert "real-gh-stdout" in result.stdout
    # Every arg — flags, values with spaces — reached the real gh intact.
    assert calls.read_text().splitlines() == [
        "api", "-i", "/repos/x/y/pulls/1", "--header", "If-None-Match: abc",
    ]
    assert not helper_log.exists(), "helper must not run for non-pr-create"


def test_passthrough_propagates_exit_code(shim_env):
    shim, env, _calls, helper_log = shim_env
    result = _run(shim, env, "pr", "view", extra_env={"GH_RC": "4"})
    assert result.returncode == 4
    assert not helper_log.exists()


def test_failed_pr_create_skips_helper_and_keeps_rc(shim_env):
    shim, env, _calls, helper_log = shim_env
    result = _run(shim, env, "pr", "create", "--fill", extra_env={"GH_RC": "1"})
    assert result.returncode == 1
    assert not helper_log.exists(), "helper must not run when pr create failed"


def test_successful_pr_create_runs_helper_once(shim_env):
    shim, env, calls, helper_log = shim_env
    result = _run(shim, env, "pr", "create", "--title", "t", "--body", "b")
    assert result.returncode == 0
    assert calls.read_text().splitlines() == [
        "pr", "create", "--title", "t", "--body", "b",
    ]
    assert helper_log.read_text().splitlines() == ["helper-ran"]
    assert "review binding declared" in result.stdout


def test_helper_failure_prompts_but_preserves_success(shim_env):
    shim, env, _calls, helper_log = shim_env
    result = _run(shim, env, "pr", "create", extra_env={"HELPER_RC": "3"})
    # The PR was created — the gh call's success must survive helper failure.
    assert result.returncode == 0
    assert helper_log.read_text().splitlines() == ["helper-ran"]
    assert "NOT linked" in result.stdout
    assert "declare-review-binding.sh" in result.stdout
