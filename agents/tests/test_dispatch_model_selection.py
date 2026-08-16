"""Acceptance tests for per-bead dispatch model selection (auto-vpo3v).

A bead may override the dispatch model with a ``model:<name>`` label. The lever
threads through the same argv path the harness already uses:

    dispatcher dispatch loop  (_resolve_bead_model on bead labels)
      → start_agent(model=...)  → launch.sh --model=X
        → launch_session_cli --model X
          → launch_session(model=...)  → claude/codex --model

Precedence (resolved inside launch_session_cli / launch_session, NOT re-decided
here): **bead label > workspace model > built-in default**. A bead label, when
present, is passed as ``--model`` and wins; with no label no flag is passed and
the existing workspace-then-default chain resolves exactly as before.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents import dispatcher, launch_session_cli, session_launcher


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_SH = REPO_ROOT / "agents" / "launch.sh"


def _completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ══════════════════════════════════════════════════════════════════════
# _resolve_bead_model — label parsing, aliasing, and typo rejection
# ══════════════════════════════════════════════════════════════════════


class TestResolveBeadModel:
    def test_no_model_label_returns_none(self):
        """No model: label → None, so the caller passes no --model and the
        workspace-then-default chain resolves untouched."""
        assert dispatcher._resolve_bead_model(["dashboard", "ui"]) is None
        assert dispatcher._resolve_bead_model([]) is None

    def test_alias_resolves_to_full_model_id(self):
        assert dispatcher._resolve_bead_model(["model:sonnet"]) == dispatcher.DEFAULT_SONNET_MODEL
        assert dispatcher._resolve_bead_model(["model:opus"]) == dispatcher.DEFAULT_OPUS_MODEL
        assert dispatcher._resolve_bead_model(["model:haiku"]) == "claude-haiku-4-5-20251001"

    def test_full_model_id_passes_through(self):
        """A label already carrying a known full id is accepted verbatim."""
        assert (
            dispatcher._resolve_bead_model([f"model:{dispatcher.DEFAULT_SONNET_MODEL}"])
            == dispatcher.DEFAULT_SONNET_MODEL
        )

    def test_unknown_model_label_raises_naming_the_label(self):
        """A typo must fail loudly — the whole point of the bead. The message
        names the offending label so the operator can find it."""
        with pytest.raises(ValueError) as exc:
            dispatcher._resolve_bead_model(["model:sonnett"])
        msg = str(exc.value)
        assert "model:sonnett" in msg
        assert "sonnett" in msg
        # It must NOT silently fall back to a default.
        assert dispatcher.DEFAULT_OPUS_MODEL not in msg or "silent" in msg.lower()


# ══════════════════════════════════════════════════════════════════════
# start_agent — forwards --model only when set
# ══════════════════════════════════════════════════════════════════════


class TestStartAgentForwardsModel:
    _STDOUT = (
        "CONTAINER_ID=abc\nCONTAINER_NAME=n\nOUTPUT_DIR=/o\n"
        "WORKTREE_DIR=/w\nBRANCH=agent/b\nBRANCH_BASE=base\n"
    )

    @patch("agents.dispatcher.subprocess.run")
    def test_model_flag_forwarded_when_set(self, mock_run):
        mock_run.return_value = _completed_process(stdout=self._STDOUT)
        dispatcher.start_agent("auto-x", model="claude-sonnet-4-6")
        argv = mock_run.call_args[0][0]
        assert "--model=claude-sonnet-4-6" in argv

    @patch("agents.dispatcher.subprocess.run")
    def test_model_flag_omitted_when_unset(self, mock_run):
        mock_run.return_value = _completed_process(stdout=self._STDOUT)
        dispatcher.start_agent("auto-x")
        argv = mock_run.call_args[0][0]
        assert not any(a.startswith("--model") for a in argv)


# ══════════════════════════════════════════════════════════════════════
# launch.sh — forwards --model at BOTH invocation sites (detach + foreground)
# ══════════════════════════════════════════════════════════════════════


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def launch_harness(tmp_path):
    """A relocated copy of launch.sh with every external it shells out to
    (python, git, docker) stubbed, so the argv it hands launch_session_cli can
    be inspected without a real container, worktree, or venv.

    The python stub records the launch_session_cli argv to ``capture.txt`` and,
    for --detach, prints the CONTAINER_ID line launch.sh greps for.
    """
    root = tmp_path / "repo"
    (root / "agents").mkdir(parents=True)
    shutil.copy(LAUNCH_SH, root / "agents" / "launch.sh")
    _make_executable(root / "agents" / "launch.sh")

    capture = tmp_path / "capture.txt"

    py = root / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "if 'agents.compose' in argv:\n"
        "    print('PROMPT BODY')\n"
        "elif 'agents.launch_session_cli' in argv:\n"
        f"    open({str(capture)!r}, 'w').write(chr(10).join(argv))\n"
        "    if '--detach' in argv:\n"
        "        print('CONTAINER_ID=fakecid')\n"
    )
    _make_executable(py)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    git = bindir / "git"
    git.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "a = sys.argv[1:]\n"
        "# strip a leading -C <dir>\n"
        "if a[:1] == ['-C']:\n"
        "    a = a[2:]\n"
        "if a[:1] == ['rev-parse'] and '--verify' in a:\n"
        "    sys.exit(1)   # branch does not exist yet\n"
        "if a[:1] == ['rev-parse']:\n"
        "    print('deadbeefdeadbeefdeadbeefdeadbeefdeadbeef')\n"
        "sys.exit(0)\n"
    )
    _make_executable(git)
    docker = bindir / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    _make_executable(docker)

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "CLAUDE_CODE_OAUTH_TOKEN": "fake-token",
    }

    def run(*extra_args):
        result = subprocess.run(
            [str(root / "agents" / "launch.sh"), "auto-model", *extra_args],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert capture.exists(), f"launch_session_cli was never invoked:\n{result.stdout}\n{result.stderr}"
        return capture.read_text().splitlines()

    return run


def _assert_model_forwarded(argv: list[str], expected: str) -> None:
    """--model and its value must be adjacent, as launch.sh emits them."""
    assert "--model" in argv, argv
    assert argv[argv.index("--model") + 1] == expected, argv


class TestLaunchShForwardsModel:
    def test_detached_path_forwards_model(self, launch_harness):
        argv = launch_harness("--harness=claude", "--model=sonnet", "--detach")
        assert "--detach" in argv          # confirms we exercised the detached site
        _assert_model_forwarded(argv, "sonnet")

    def test_foreground_path_forwards_model(self, launch_harness):
        argv = launch_harness("--harness=claude", "--model=sonnet")
        assert "--detach" not in argv      # confirms we exercised the foreground site
        _assert_model_forwarded(argv, "sonnet")

    def test_no_model_flag_when_omitted_detached(self, launch_harness):
        argv = launch_harness("--harness=claude", "--detach")
        assert "--model" not in argv

    def test_no_model_flag_when_omitted_foreground(self, launch_harness):
        argv = launch_harness("--harness=claude")
        assert "--model" not in argv


# ══════════════════════════════════════════════════════════════════════
# Three-scenario precedence at the launch_session_cli → launch_session seam
# (detach path — the one the dispatcher uses): label > workspace > default.
# ══════════════════════════════════════════════════════════════════════


class TestModelPrecedence:
    def _invoke_detach(self, tmp_path, *, argv_extra, workspace=None):
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("prompt body")
        output_dir = tmp_path / "run"
        output_dir.mkdir()

        captured: dict = {}

        def fake_launch_session(**kwargs):
            captured.update(kwargs)
            return "fake-container-id"

        argv = [
            "launch_session_cli",
            "--session-type", "dispatch",
            "--name", "agent-auto-model-1234",
            "--prompt-file", str(prompt_file),
            "--bead-id", "auto-model",
            "--output-dir", str(output_dir),
            "--image", "autonomy-agent",
            "--harness", "claude",
            "--org", "autonomy",
            "--detach",
            *argv_extra,
        ]
        workspaces = {"ws-1": workspace} if workspace is not None else {}
        with patch.object(launch_session_cli, "load_workspaces", return_value=workspaces):
            with patch.object(launch_session_cli, "launch_session", fake_launch_session):
                with patch("sys.argv", argv):
                    rc = launch_session_cli.main()
        assert rc == 0
        return captured

    def _workspace(self, model):
        return SimpleNamespace(
            graph_project="autonomy", model=model,
            needs_nested_docker=False, session_runtime="standard",
        )

    def test_bead_label_beats_workspace_model(self, tmp_path):
        """Scenario 1 + precedence: a resolved model label (passed as --model)
        wins over the workspace's declared model."""
        captured = self._invoke_detach(
            tmp_path,
            argv_extra=["--workspace-id", "ws-1", "--model", "claude-sonnet-4-6"],
            workspace=self._workspace("claude-opus-4-8[1m]"),
        )
        assert captured["model"] == "claude-sonnet-4-6"

    def test_workspace_model_used_when_no_label(self, tmp_path):
        """Scenario 2 + precedence: with no --model, the workspace model is
        used — it beats the built-in default (which would be None here)."""
        captured = self._invoke_detach(
            tmp_path,
            argv_extra=["--workspace-id", "ws-1"],
            workspace=self._workspace("claude-opus-4-8[1m]"),
        )
        assert captured["model"] == "claude-opus-4-8[1m]"

    def test_no_label_no_workspace_model_falls_to_default(self, tmp_path):
        """Scenario 3: no label and a workspace declaring no model → launch_cli
        passes model=None, and launch_session fills the built-in default."""
        captured = self._invoke_detach(
            tmp_path,
            argv_extra=["--workspace-id", "ws-1"],
            workspace=self._workspace(None),
        )
        assert captured["model"] is None


class TestLaunchSessionDefaultsToOpus:
    """The built-in default lives in session_launcher: a claude launch with
    model=None resolves to DEFAULT_OPUS_MODEL. Proves scenario-3's tail —
    ``model=None`` really does dispatch with the default, not an empty model."""

    @pytest.fixture
    def stubbed_docker(self, monkeypatch):
        calls: list[list[str]] = []
        completed = _completed_process(stdout="fake-container-id\n")

        def fake_run(cmd, **kwargs):
            if (cmd and cmd[0] == "docker"
                    and not (len(cmd) > 2 and cmd[1] == "image" and cmd[2] == "inspect")):
                calls.append(cmd)
            return completed

        monkeypatch.setattr(session_launcher.subprocess, "run", fake_run)
        monkeypatch.setattr(
            session_launcher, "_resolve_credentials",
            lambda: {"type": "token", "token": "tok"},
        )
        fake_dao = SimpleNamespace(auth_db=SimpleNamespace(insert_token=lambda *a, **k: None))
        monkeypatch.setitem(sys.modules, "tools.dashboard.dao", fake_dao)
        return calls

    def test_claude_none_model_uses_default_opus(self, tmp_path, stubbed_docker):
        session_launcher.launch_session(
            session_type="dispatch",
            name="agent-auto-model-1234",
            prompt=None,
            detach=True,
            image="autonomy-agent",
            output_dir=str(tmp_path / "run"),
            harness="claude",
            model=None,
            metadata={"bead_id": "auto-model", "org": "autonomy"},
        )
        cmd = stubbed_docker[0]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == session_launcher.DEFAULT_OPUS_MODEL


# ══════════════════════════════════════════════════════════════════════
# The dispatcher no longer keeps a second copy of DEFAULT_OPUS_MODEL.
# ══════════════════════════════════════════════════════════════════════


def test_dispatcher_reuses_session_launcher_opus_constant():
    """The dead duplicate was retired: dispatcher.DEFAULT_OPUS_MODEL is the very
    object launch_session_cli resolves against, not a look-alike copy."""
    assert dispatcher.DEFAULT_OPUS_MODEL is session_launcher.DEFAULT_OPUS_MODEL


# ══════════════════════════════════════════════════════════════════════
# Version-suffixed aliases: naming a model exactly, without moving the
# default out from under every unlabelled bead.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "label,expected",
    [
        ("model:opus-5", "claude-opus-5"),
        ("model:sonnet-5", "claude-sonnet-5"),
        ("model:fable-5", "claude-fable-5"),
    ],
)
def test_version_suffixed_alias_resolves(label, expected):
    assert dispatcher._resolve_bead_model([label]) == expected


@pytest.mark.parametrize(
    "label,expected",
    [
        ("model:claude-opus-5", "claude-opus-5"),
        ("model:claude-sonnet-5", "claude-sonnet-5"),
    ],
)
def test_full_model_id_resolves_without_its_own_key(label, expected):
    """Adding a model as a VALUE also makes its full id nameable.

    ``_resolve_bead_model`` accepts anything present in ``MODEL_ALIASES``'s
    values, so one entry serves both ``model:opus-5`` and the explicit
    ``model:claude-opus-5`` — no second key, no chance of the two drifting.
    """
    assert dispatcher._resolve_bead_model([label]) == expected


def test_bare_family_aliases_are_pinned_not_latest():
    """The invariant that makes adding a model safe.

    ``opus`` is DEFAULT_OPUS_MODEL, which is ALSO what an unlabelled bead
    runs. Repointing it at each new release would silently change the model
    for every bead that names nothing, so the bare aliases stay pinned and
    new versions get their own key. If this test fails, adding a model moved
    the default — which is the failure the version-suffixed keys exist to
    prevent.
    """
    assert dispatcher._resolve_bead_model(["model:opus"]) == session_launcher.DEFAULT_OPUS_MODEL
    assert dispatcher._resolve_bead_model(["model:sonnet"]) == dispatcher.DEFAULT_SONNET_MODEL
    # And the no-label path is untouched: no --model flag at all.
    assert dispatcher._resolve_bead_model(["readiness:approved"]) is None


def test_unregistered_version_still_fails_loudly():
    """A plausible-looking typo must stop the dispatch, not run the default."""
    with pytest.raises(ValueError) as exc:
        dispatcher._resolve_bead_model(["model:opus-9"])
    assert "opus-9" in str(exc.value)
    assert "opus-5" in str(exc.value)  # the message lists what IS valid
