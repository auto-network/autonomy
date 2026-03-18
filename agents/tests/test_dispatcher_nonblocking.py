"""Tests for non-blocking dispatcher — start/poll/collect lifecycle."""

from unittest.mock import MagicMock, patch, call
import subprocess
import time
import json
from pathlib import Path

import pytest

from agents.dispatcher import (
    RunningAgent,
    DispatchResult,
    DispatcherConfig,
    start_agent,
    poll_container,
    collect_results,
    poll_and_collect,
    dispatch_cycle,
    recover_running_agents,
    get_open_dependencies,
    REPO_ROOT,
)


def _completed_process(stdout="", stderr="", returncode=0):
    """Create a mock CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_running_agent(**overrides):
    """Create a RunningAgent with defaults."""
    defaults = dict(
        bead_id="auto-test",
        container_name="agent-auto-test-1234",
        container_id="abc123def456",
        output_dir="/tmp/test-output",
        worktree_path="/tmp/test-worktree",
        branch="agent/auto-test",
        branch_base="aaa111",
        image="autonomy-agent",
        started_at=time.time(),
    )
    defaults.update(overrides)
    return RunningAgent(**defaults)


# ── poll_container ──────────────────────────────────────────────


class TestPollContainer:
    @patch("agents.dispatcher.subprocess.run")
    def test_running_container(self, mock_run):
        mock_run.return_value = _completed_process(stdout="running 0")
        finished, exit_code = poll_container("abc123")
        assert finished is False
        assert exit_code == -1

    @patch("agents.dispatcher.subprocess.run")
    def test_exited_container_success(self, mock_run):
        mock_run.return_value = _completed_process(stdout="exited 0")
        finished, exit_code = poll_container("abc123")
        assert finished is True
        assert exit_code == 0

    @patch("agents.dispatcher.subprocess.run")
    def test_exited_container_failure(self, mock_run):
        mock_run.return_value = _completed_process(stdout="exited 1")
        finished, exit_code = poll_container("abc123")
        assert finished is True
        assert exit_code == 1

    @patch("agents.dispatcher.subprocess.run")
    def test_container_not_found(self, mock_run):
        """docker inspect failure (e.g. container removed) → unknown, retry next cycle."""
        mock_run.return_value = _completed_process(returncode=1, stderr="No such object")
        finished, exit_code = poll_container("abc123")
        assert finished is False
        assert exit_code == -1

    @patch("agents.dispatcher.subprocess.run")
    def test_dead_container(self, mock_run):
        mock_run.return_value = _completed_process(stdout="dead 137")
        finished, exit_code = poll_container("abc123")
        assert finished is True
        assert exit_code == 137

    @patch("agents.dispatcher.subprocess.run")
    def test_timeout_returns_not_finished(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=10)
        finished, exit_code = poll_container("abc123")
        assert finished is False


# ── start_agent ─────────────────────────────────────────────────


class TestStartAgent:
    @patch("agents.dispatcher.subprocess.run")
    def test_successful_launch(self, mock_run):
        """launch.sh --detach returns key=value metadata."""
        stdout = (
            "==> Generating prompt for auto-xyz...\n"
            "==> Launching agent container: agent-auto-xyz-9999\n"
            "CONTAINER_ID=abc123def456789\n"
            "CONTAINER_NAME=agent-auto-xyz-9999\n"
            "OUTPUT_DIR=/repo/data/agent-runs/auto-xyz-20260317\n"
            "WORKTREE_DIR=/repo/.worktrees/auto-xyz-20260317\n"
            "BRANCH=agent/auto-xyz\n"
            "BRANCH_BASE=aaa111bbb222\n"
        )
        mock_run.return_value = _completed_process(stdout=stdout)

        agent = start_agent("auto-xyz", image="autonomy-agent")
        assert agent is not None
        assert agent.bead_id == "auto-xyz"
        assert agent.container_id == "abc123def456789"
        assert agent.container_name == "agent-auto-xyz-9999"
        assert agent.output_dir == "/repo/data/agent-runs/auto-xyz-20260317"
        assert agent.worktree_path == "/repo/.worktrees/auto-xyz-20260317"
        assert agent.branch == "agent/auto-xyz"
        assert agent.branch_base == "aaa111bbb222"
        assert agent.started_at > 0

    @patch("agents.dispatcher.subprocess.run")
    def test_launch_failure_returns_none(self, mock_run):
        mock_run.return_value = _completed_process(
            returncode=1, stderr="Docker image not found"
        )
        agent = start_agent("auto-fail")
        assert agent is None

    @patch("agents.dispatcher.subprocess.run")
    def test_launch_timeout_returns_none(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=120)
        agent = start_agent("auto-timeout")
        assert agent is None

    @patch("agents.dispatcher.subprocess.run")
    def test_missing_metadata_returns_none(self, mock_run):
        """launch.sh succeeds but output doesn't contain expected metadata."""
        mock_run.return_value = _completed_process(stdout="some unexpected output\n")
        agent = start_agent("auto-bad")
        assert agent is None

    @patch("agents.dispatcher.subprocess.run")
    def test_passes_detach_flag(self, mock_run):
        mock_run.return_value = _completed_process(
            stdout="CONTAINER_ID=x\nOUTPUT_DIR=/y\nCONTAINER_NAME=z\n"
        )
        start_agent("auto-check", image="test-image")
        args = mock_run.call_args[0][0]
        assert "--detach" in args
        assert "--image=test-image" in args


# ── collect_results ─────────────────────────────────────────────


class TestCollectResults:
    @patch("agents.dispatcher.remove_container")
    @patch("agents.dispatcher.subprocess.run")
    def test_collects_decision_and_commit(self, mock_run, mock_rm, tmp_path):
        """Reads decision.json, detects new commits, cleans up container."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "decision.json").write_text(
            json.dumps({"status": "DONE", "reason": "completed"})
        )

        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        agent = _make_running_agent(
            output_dir=str(output_dir),
            worktree_path=str(worktree_dir),
            branch_base="aaa111",
        )

        # git rev-parse HEAD returns a different hash
        mock_run.return_value = _completed_process(stdout="bbb222")

        result = collect_results(agent, exit_code=0)

        assert result.decision == {"status": "DONE", "reason": "completed"}
        assert result.commit_hash == "bbb222"
        assert result.exit_code == 0
        assert result.bead_id == agent.bead_id
        mock_rm.assert_called_once_with(agent.container_name)

    @patch("agents.dispatcher.remove_container")
    @patch("agents.dispatcher.subprocess.run")
    def test_no_decision_no_commit(self, mock_run, mock_rm, tmp_path):
        """No decision.json, no new commits."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        agent = _make_running_agent(
            output_dir=str(output_dir),
            worktree_path=str(worktree_dir),
            branch_base="aaa111",
        )

        # HEAD matches branch_base — no new commits
        mock_run.return_value = _completed_process(stdout="aaa111")

        result = collect_results(agent, exit_code=1)

        assert result.decision is None
        assert result.commit_hash == ""
        assert result.exit_code == 1
        assert "exited with code 1" in result.error

    @patch("agents.dispatcher.remove_container")
    @patch("agents.dispatcher.subprocess.run")
    def test_cleans_up_detach_temp_files(self, mock_run, mock_rm, tmp_path):
        """Removes .credentials.json and .prompt.md from output dir."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / ".credentials.json").write_text("secret")
        (output_dir / ".prompt.md").write_text("prompt")

        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        agent = _make_running_agent(
            output_dir=str(output_dir),
            worktree_path=str(worktree_dir),
        )
        mock_run.return_value = _completed_process(stdout=agent.branch_base)

        collect_results(agent, exit_code=0)

        assert not (output_dir / ".credentials.json").exists()
        assert not (output_dir / ".prompt.md").exists()


# ── poll_and_collect ────────────────────────────────────────────


class TestPollAndCollect:
    @patch("agents.dispatcher._ingest_session")
    @patch("agents.dispatcher.process_decision")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.poll_container")
    def test_completed_agent_collected(self, mock_poll,
                                        mock_collect, mock_process, mock_ingest):
        """Completed agent is removed from running and processed."""
        agent = _make_running_agent()
        running = [agent]

        mock_poll.return_value = (True, 0)
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id, exit_code=0
        )

        poll_and_collect(running)

        assert len(running) == 0
        mock_collect.assert_called_once_with(agent, 0)
        mock_process.assert_called_once()
        mock_ingest.assert_called_once()

    @patch("agents.dispatcher.poll_container")
    def test_still_running_not_collected(self, mock_poll):
        """Running agent stays in the list."""
        agent = _make_running_agent()
        running = [agent]

        mock_poll.return_value = (False, -1)

        poll_and_collect(running)

        assert len(running) == 1
        assert running[0] is agent

    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.kill_container")
    @patch("agents.dispatcher.poll_container")
    def test_timed_out_agent_killed(self, mock_poll, mock_kill,
                                     mock_collect,
                                     mock_release, mock_cleanup):
        """Agent exceeding MAX_AGENT_RUNTIME is killed and released."""
        agent = _make_running_agent(
            started_at=time.time() - 9999  # Way past timeout
        )
        running = [agent]

        mock_poll.return_value = (False, -1)
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id, exit_code=-1,
            decision=None, commit_hash="",
        )

        poll_and_collect(running)

        assert len(running) == 0
        mock_kill.assert_called_once_with(agent.container_name)
        mock_release.assert_called_once()

    @patch("agents.dispatcher._ingest_session")
    @patch("agents.dispatcher.process_decision")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.kill_container")
    @patch("agents.dispatcher.poll_container")
    def test_timed_out_agent_with_results_processed(self, mock_poll, mock_kill,
                                                     mock_collect,
                                                     mock_process, mock_ingest):
        """Timed out agent with partial results gets processed normally."""
        agent = _make_running_agent(
            started_at=time.time() - 9999
        )
        running = [agent]

        mock_poll.return_value = (False, -1)
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id, exit_code=-1,
            decision={"status": "DONE", "reason": "finished before timeout"},
            commit_hash="abc123",
        )

        poll_and_collect(running)

        assert len(running) == 0
        mock_kill.assert_called_once()
        mock_process.assert_called_once()


# ── dispatch_cycle ──────────────────────────────────────────────


class TestDispatchCycle:
    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_launches_agent_when_slots_available(
        self, mock_poll, mock_ready, mock_claimed, mock_start
    ):
        """With empty running list and ready beads, launches an agent."""
        running = []
        config = DispatcherConfig(max_concurrent=1)

        mock_ready.return_value = [
            {"id": "auto-abc", "title": "Test", "priority": 1}
        ]
        mock_claimed.return_value = set()
        mock_start.return_value = _make_running_agent(bead_id="auto-abc")

        dispatched = dispatch_cycle(config, running)

        assert dispatched == 1
        assert len(running) == 1
        assert running[0].bead_id == "auto-abc"

    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_at_capacity_no_launch(self, mock_poll, mock_ready):
        """Does not launch when at max_concurrent."""
        running = [_make_running_agent()]
        config = DispatcherConfig(max_concurrent=1)

        dispatched = dispatch_cycle(config, running)

        assert dispatched == 0
        mock_ready.assert_not_called()  # Shouldn't even query beads

    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_multiple_slots_dispatches_multiple(
        self, mock_poll, mock_ready, mock_claimed, mock_start
    ):
        """With max_concurrent=3 and 2 available beads, launches 2."""
        running = []
        config = DispatcherConfig(max_concurrent=3)

        mock_ready.return_value = [
            {"id": "auto-a", "title": "A", "priority": 1},
            {"id": "auto-b", "title": "B", "priority": 2},
        ]
        mock_claimed.return_value = set()
        mock_start.side_effect = [
            _make_running_agent(bead_id="auto-a"),
            _make_running_agent(bead_id="auto-b"),
        ]

        dispatched = dispatch_cycle(config, running)

        assert dispatched == 2
        assert len(running) == 2

    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_no_ready_beads(self, mock_poll, mock_ready):
        """With no approved beads, dispatches nothing."""
        running = []
        config = DispatcherConfig()

        mock_ready.return_value = []

        dispatched = dispatch_cycle(config, running)
        assert dispatched == 0

    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_dry_run_no_launch(
        self, mock_poll, mock_ready, mock_claimed, mock_start
    ):
        """Dry run shows bead but doesn't launch."""
        running = []
        config = DispatcherConfig(dry_run=True)

        mock_ready.return_value = [
            {"id": "auto-dry", "title": "Dry", "priority": 0}
        ]
        mock_claimed.return_value = set()

        dispatched = dispatch_cycle(config, running)

        assert dispatched == 0
        mock_start.assert_not_called()

    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher.find_worktree_for_bead")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_launch_failure_releases_bead(
        self, mock_poll, mock_ready, mock_claimed,
        mock_start, mock_release, mock_find_wt, mock_cleanup
    ):
        """If start_agent fails, bead is released as FAILED."""
        running = []
        config = DispatcherConfig()

        mock_ready.return_value = [
            {"id": "auto-fail", "title": "Fail", "priority": 1}
        ]
        mock_claimed.return_value = set()
        mock_start.return_value = None
        mock_find_wt.return_value = ""

        dispatch_cycle(config, running)

        assert len(running) == 0
        mock_release.assert_called_once_with(
            "auto-fail", "FAILED", "Container launch failed"
        )


# ── recover_running_agents ──────────────────────────────────────


class TestRecoverRunningAgents:
    @patch("agents.dispatcher.subprocess.run")
    def test_no_containers(self, mock_run):
        mock_run.return_value = _completed_process(stdout="")
        agents = recover_running_agents()
        assert agents == []

    @patch("agents.dispatcher.subprocess.run")
    def test_recovers_running_container(self, mock_run, tmp_path):
        """Recovers agent metadata from docker ps + output dir."""
        mock_run.return_value = _completed_process(
            stdout="abc123 agent-auto-xyz-5555 Up 5 minutes"
        )

        # Create a fake output dir
        runs_dir = tmp_path / "data" / "agent-runs"
        runs_dir.mkdir(parents=True)
        output_dir = runs_dir / "auto-xyz-20260317-120000"
        output_dir.mkdir()
        (output_dir / ".branch_base").write_text("aaa111")
        (output_dir / ".worktree_path").write_text("/tmp/wt")
        (output_dir / ".branch").write_text("agent/auto-xyz")

        # Patch REPO_ROOT to our tmp_path
        with patch("agents.dispatcher.REPO_ROOT", tmp_path):
            agents = recover_running_agents()

        assert len(agents) == 1
        assert agents[0].bead_id == "auto-xyz"
        assert agents[0].container_name == "agent-auto-xyz-5555"
        assert agents[0].branch == "agent/auto-xyz"


# ── get_open_dependencies ──────────────────────────────────────


class TestGetOpenDependencies:
    @patch("agents.dispatcher.run_bd")
    def test_no_dependencies(self, mock_bd):
        """Bead with no deps returns empty list."""
        mock_bd.return_value = "[]"
        result = get_open_dependencies("auto-abc")
        assert result == []

    @patch("agents.dispatcher.run_bd")
    def test_all_deps_closed(self, mock_bd):
        """All blocking deps closed — returns empty list."""
        mock_bd.return_value = json.dumps([
            {"id": "auto-dep1", "status": "closed", "dependency_type": "blocks"},
            {"id": "auto-dep2", "status": "closed", "dependency_type": "blocks"},
        ])
        result = get_open_dependencies("auto-abc")
        assert result == []

    @patch("agents.dispatcher.run_bd")
    def test_open_blocking_dep(self, mock_bd):
        """Open blocking dep is returned."""
        mock_bd.return_value = json.dumps([
            {"id": "auto-dep1", "status": "open", "dependency_type": "blocks"},
        ])
        result = get_open_dependencies("auto-abc")
        assert len(result) == 1
        assert result[0]["id"] == "auto-dep1"

    @patch("agents.dispatcher.run_bd")
    def test_in_progress_blocking_dep(self, mock_bd):
        """In-progress blocking dep is returned (not closed yet)."""
        mock_bd.return_value = json.dumps([
            {"id": "auto-dep1", "status": "in_progress", "dependency_type": "blocks"},
        ])
        result = get_open_dependencies("auto-abc")
        assert len(result) == 1

    @patch("agents.dispatcher.run_bd")
    def test_parent_child_dep_ignored(self, mock_bd):
        """Parent-child deps never block dispatch."""
        mock_bd.return_value = json.dumps([
            {"id": "auto-epic", "status": "open", "dependency_type": "parent-child"},
        ])
        result = get_open_dependencies("auto-abc")
        assert result == []

    @patch("agents.dispatcher.run_bd")
    def test_mixed_deps(self, mock_bd):
        """Only open blocking deps are returned; closed and parent-child are excluded."""
        mock_bd.return_value = json.dumps([
            {"id": "auto-dep1", "status": "closed", "dependency_type": "blocks"},
            {"id": "auto-dep2", "status": "open", "dependency_type": "blocks"},
            {"id": "auto-epic", "status": "open", "dependency_type": "parent-child"},
        ])
        result = get_open_dependencies("auto-abc")
        assert len(result) == 1
        assert result[0]["id"] == "auto-dep2"

    @patch("agents.dispatcher.run_bd")
    def test_bd_returns_empty(self, mock_bd):
        """bd dep list returns empty string (command failure)."""
        mock_bd.return_value = ""
        result = get_open_dependencies("auto-abc")
        assert result == []

    @patch("agents.dispatcher.run_bd")
    def test_bd_returns_invalid_json(self, mock_bd):
        """bd dep list returns malformed output."""
        mock_bd.return_value = "not json"
        result = get_open_dependencies("auto-abc")
        assert result == []


# ── dispatch_cycle dependency filtering ─────────────────────────


class TestDispatchCycleDependencyFiltering:
    @patch("agents.dispatcher.get_open_dependencies")
    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_skips_bead_with_open_deps(
        self, mock_poll, mock_ready, mock_claimed,
        mock_start, mock_open_deps
    ):
        """Bead with open blocking dependency is NOT dispatched."""
        running = []
        config = DispatcherConfig(max_concurrent=2)

        mock_ready.return_value = [
            {"id": "auto-blocked", "title": "Blocked", "priority": 1,
             "dependency_count": 1},
            {"id": "auto-free", "title": "Free", "priority": 2,
             "dependency_count": 0},
        ]
        mock_claimed.return_value = set()
        mock_start.return_value = _make_running_agent(bead_id="auto-free")

        # auto-blocked has an open dep, auto-free has none (skipped due to dep_count=0)
        mock_open_deps.return_value = [
            {"id": "auto-prereq", "status": "open", "dependency_type": "blocks"}
        ]

        dispatched = dispatch_cycle(config, running)

        # Only auto-free should be dispatched
        assert dispatched == 1
        assert len(running) == 1
        assert running[0].bead_id == "auto-free"

        # get_open_dependencies should only be called for the bead with deps
        mock_open_deps.assert_called_once_with("auto-blocked")

    @patch("agents.dispatcher.get_open_dependencies")
    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_all_beads_blocked_by_deps(
        self, mock_poll, mock_ready, mock_claimed,
        mock_start, mock_open_deps
    ):
        """When all beads have open deps, nothing is dispatched."""
        running = []
        config = DispatcherConfig()

        mock_ready.return_value = [
            {"id": "auto-a", "title": "A", "priority": 1, "dependency_count": 1},
        ]
        mock_claimed.return_value = set()
        mock_open_deps.return_value = [
            {"id": "auto-prereq", "status": "open", "dependency_type": "blocks"}
        ]

        dispatched = dispatch_cycle(config, running)

        assert dispatched == 0
        assert len(running) == 0
        mock_start.assert_not_called()

    @patch("agents.dispatcher.get_open_dependencies")
    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_bead_with_closed_deps_dispatched(
        self, mock_poll, mock_ready, mock_claimed,
        mock_start, mock_open_deps
    ):
        """Bead whose deps are all closed gets dispatched normally."""
        running = []
        config = DispatcherConfig()

        mock_ready.return_value = [
            {"id": "auto-ok", "title": "Ready", "priority": 1,
             "dependency_count": 2},
        ]
        mock_claimed.return_value = set()
        mock_open_deps.return_value = []  # All deps closed
        mock_start.return_value = _make_running_agent(bead_id="auto-ok")

        dispatched = dispatch_cycle(config, running)

        assert dispatched == 1
        assert len(running) == 1
        mock_open_deps.assert_called_once_with("auto-ok")

    @patch("agents.dispatcher.get_open_dependencies")
    @patch("agents.dispatcher.start_agent")
    @patch("agents.dispatcher.get_claimed_beads")
    @patch("agents.dispatcher.get_ready_beads")
    @patch("agents.dispatcher.poll_and_collect")
    def test_bead_with_no_dep_count_but_inline_deps(
        self, mock_poll, mock_ready, mock_claimed,
        mock_start, mock_open_deps
    ):
        """Bead with dependencies inline in JSON is checked even without dependency_count."""
        running = []
        config = DispatcherConfig()

        mock_ready.return_value = [
            {"id": "auto-inline", "title": "Inline deps", "priority": 1,
             "dependency_count": 0,
             "dependencies": [{"id": "auto-dep", "dependency_type": "blocks"}]},
        ]
        mock_claimed.return_value = set()
        mock_open_deps.return_value = [
            {"id": "auto-dep", "status": "open", "dependency_type": "blocks"}
        ]

        dispatched = dispatch_cycle(config, running)

        assert dispatched == 0
        mock_open_deps.assert_called_once_with("auto-inline")
