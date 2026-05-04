"""Regression tests for the auto-qjme3 contradiction path.

A bead can land DONE with a real merged commit and then trip the
post-land collection step (``graph sessions --all`` timing out inside
``_ingest_session``). Before the fix, that exception escaped the broad
``except`` in :func:`poll_and_collect` and called
``release_bead(bead, "FAILED")`` — reopening a bead whose implementation
had already landed on master/hostsync.

These tests pin the new contract:

1. ``_ingest_session_safe`` must swallow any exception from
   ``_ingest_session``, never re-raise.
2. When ``_ingest_session`` raises after ``process_decision`` returned
   DONE, the bead must NOT be reopened (no ``release_bead`` call with
   FAILED), the recorded run must keep DONE status, and the failure must
   surface as a separate warning note via ``run_bd``.
3. When the failure is the exact symptom from auto-qjme3
   (``TimeoutExpired`` on ``graph sessions --all``), DONE is preserved.
"""

from unittest.mock import MagicMock, patch
import subprocess
import time

from agents.dispatcher import (
    DispatchResult,
    RunningAgent,
    _ingest_session_safe,
    poll_and_collect,
)


def _make_running_agent(**overrides):
    defaults = dict(
        bead_id="auto-qjme3",
        container_name="agent-auto-qjme3-1234",
        container_id="abc123",
        output_dir="/tmp/test-output",
        worktree_path="/tmp/test-worktree",
        branch="agent/auto-qjme3",
        branch_base="aaa111",
        image="autonomy-agent",
        started_at=time.time(),
    )
    defaults.update(overrides)
    return RunningAgent(**defaults)


# ── _ingest_session_safe contract ──────────────────────────────────────


class TestIngestSessionSafe:
    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher._ingest_session")
    def test_swallows_timeout_expired(self, mock_ingest, mock_bd):
        """The exact qjme3 symptom — TimeoutExpired on graph sessions --all —
        must not propagate out of the safe wrapper."""
        mock_ingest.side_effect = subprocess.TimeoutExpired(
            cmd=["graph", "sessions", "--all"], timeout=30
        )
        agent = _make_running_agent()
        result = DispatchResult(
            bead_id=agent.bead_id, exit_code=0, commit_hash="4d9fb0c"
        )

        _ingest_session_safe(agent, result, "DONE")

        mock_ingest.assert_called_once_with(result)

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher._ingest_session")
    def test_swallows_arbitrary_exceptions(self, mock_ingest, mock_bd):
        """Generic exceptions are also non-raising — collection must never
        escalate to bead state change."""
        mock_ingest.side_effect = RuntimeError("kaboom")
        agent = _make_running_agent()
        result = DispatchResult(bead_id=agent.bead_id, exit_code=0)

        _ingest_session_safe(agent, result, "DONE")  # no raise

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher._ingest_session")
    def test_appends_warning_note_on_failure(self, mock_ingest, mock_bd):
        """A post-land ingest failure must surface as a separate warning
        annotation on the bead, with the landed commit unaffected."""
        mock_ingest.side_effect = subprocess.TimeoutExpired(
            cmd=["graph", "sessions", "--all"], timeout=30
        )
        agent = _make_running_agent()
        result = DispatchResult(
            bead_id=agent.bead_id, exit_code=0, commit_hash="4d9fb0c"
        )

        _ingest_session_safe(agent, result, "DONE")

        # bd update --append-notes <warning>
        assert mock_bd.called, "expected warning to be appended via run_bd"
        args = mock_bd.call_args.args[0]
        assert args[0] == "update"
        assert args[1] == agent.bead_id
        assert args[2] == "--append-notes"
        warning = args[3]
        assert "post-land ingest warning" in warning
        assert "TimeoutExpired" in warning

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher._ingest_session")
    def test_run_bd_failure_also_swallowed(self, mock_ingest, mock_bd):
        """If even the warning-note bd call fails, we still don't raise —
        the wrapper's whole point is best-effort post-land cleanup."""
        mock_ingest.side_effect = subprocess.TimeoutExpired(
            cmd=["graph", "sessions", "--all"], timeout=30
        )
        mock_bd.side_effect = RuntimeError("bd unavailable")
        agent = _make_running_agent()
        result = DispatchResult(bead_id=agent.bead_id, exit_code=0)

        _ingest_session_safe(agent, result, "DONE")  # no raise

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher._ingest_session")
    def test_success_path_does_not_append_note(self, mock_ingest, mock_bd):
        """When _ingest_session succeeds the wrapper is transparent —
        no spurious warning notes."""
        agent = _make_running_agent()
        result = DispatchResult(bead_id=agent.bead_id, exit_code=0)

        _ingest_session_safe(agent, result, "DONE")

        mock_ingest.assert_called_once_with(result)
        mock_bd.assert_not_called()


# ── poll_and_collect end-to-end contradiction guard ────────────────────


class TestPollAndCollectContradictionGuard:
    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher._record_run")
    @patch("agents.dispatcher._notify_dispatch_nag")
    @patch("agents.dispatcher._update_merge_failure_counter")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher._ingest_session")
    @patch("agents.dispatcher.process_decision")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.poll_container")
    def test_landed_done_not_reopened_when_ingest_times_out(
        self,
        mock_poll,
        mock_collect,
        mock_process,
        mock_ingest,
        mock_release,
        mock_counter,
        mock_nag,
        mock_record,
        mock_cleanup,
        mock_bd,
    ):
        """Reproduces auto-qjme3: agent exits 0, decision DONE, merge lands,
        then ``graph sessions --all`` times out. ``release_bead`` must NOT
        be called from poll_and_collect (process_decision owns that), and
        the recorded run must keep DONE."""
        agent = _make_running_agent()
        running = [agent]

        mock_poll.return_value = (True, 0)
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id,
            exit_code=0,
            decision={"status": "DONE", "reason": "all checks pass"},
            commit_hash="4d9fb0c",
            branch=agent.branch,
        )
        mock_process.return_value = "DONE"
        mock_ingest.side_effect = subprocess.TimeoutExpired(
            cmd=["graph", "sessions", "--all"], timeout=30
        )

        poll_and_collect(running)

        assert running == []
        # process_decision is the single bead-state authority during the
        # post-decision phase. The poll loop must not call release_bead
        # itself when ingest fails after a landed merge.
        mock_release.assert_not_called()
        # Recorded run keeps DONE — exactly one call, with effective_status DONE.
        mock_record.assert_called_once()
        kwargs = mock_record.call_args.kwargs
        assert kwargs.get("effective_status") == "DONE"
        # Warning surfaces as a bead note rather than a state flip.
        assert any(
            call.args
            and call.args[0]
            and call.args[0][0] == "update"
            and call.args[0][1] == agent.bead_id
            and call.args[0][2] == "--append-notes"
            and "post-land ingest warning" in call.args[0][3]
            for call in mock_bd.call_args_list
        ), "expected post-land ingest warning to be appended via run_bd"

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher._record_run")
    @patch("agents.dispatcher._notify_dispatch_nag")
    @patch("agents.dispatcher._update_merge_failure_counter")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher._ingest_session")
    @patch("agents.dispatcher.process_decision")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.poll_container")
    def test_pre_decision_failure_still_reopens_as_failed(
        self,
        mock_poll,
        mock_collect,
        mock_process,
        mock_ingest,
        mock_release,
        mock_counter,
        mock_nag,
        mock_record,
        mock_cleanup,
        mock_bd,
    ):
        """Counter-test: when collect_results / process_decision themselves
        raise (genuine pre-merge failure), the bead IS still reopened as
        FAILED. The fix must not mute that path."""
        agent = _make_running_agent()
        running = [agent]

        mock_poll.return_value = (True, 0)
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id, exit_code=0
        )
        mock_process.side_effect = RuntimeError("merge_branch crashed")

        poll_and_collect(running)

        assert running == []
        mock_release.assert_called_once()
        args = mock_release.call_args.args
        assert args[0] == agent.bead_id
        assert args[1] == "FAILED"
        # ingest never reached
        mock_ingest.assert_not_called()

    @patch("agents.dispatcher.run_bd")
    @patch("agents.dispatcher.cleanup_worktree")
    @patch("agents.dispatcher._record_run")
    @patch("agents.dispatcher._notify_dispatch_nag")
    @patch("agents.dispatcher._update_merge_failure_counter")
    @patch("agents.dispatcher.release_bead")
    @patch("agents.dispatcher._ingest_session")
    @patch("agents.dispatcher.process_decision")
    @patch("agents.dispatcher.collect_results")
    @patch("agents.dispatcher.poll_container")
    def test_review_enqueue_failure_does_not_reopen_done_bead(
        self,
        mock_poll,
        mock_collect,
        mock_process,
        mock_ingest,
        mock_release,
        mock_counter,
        mock_nag,
        mock_record,
        mock_cleanup,
        mock_bd,
    ):
        """Sister case: post-decision review_report enqueue is already
        wrapped, but exercise the path to lock in that no
        release_bead("FAILED") fires when post-decision side effects
        misbehave."""
        agent = _make_running_agent()
        running = [agent]

        mock_poll.return_value = (True, 0)
        mock_collect.return_value = DispatchResult(
            bead_id=agent.bead_id,
            exit_code=0,
            decision={"status": "DONE", "reason": "ok"},
            commit_hash="4d9fb0c",
            branch=agent.branch,
        )
        mock_process.return_value = "DONE"
        # ingest is fine here; review enqueue blows up via the inner
        # try/except inside poll_and_collect (already-existing safety).
        with patch(
            "agents.dispatcher.enqueue_job",
            side_effect=RuntimeError("librarian db down"),
        ):
            poll_and_collect(running)

        mock_release.assert_not_called()
        kwargs = mock_record.call_args.kwargs
        assert kwargs.get("effective_status") == "DONE"
