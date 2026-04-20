"""auto-8bnq0 Defect 1 — reconcile must check decision.json before FAIL.

Today, on dispatcher startup:
  if dispatch_runs.status == 'RUNNING' and container not in docker ps:
      mark_failed(reason="orphaned: no container at startup")

This loses completed work when the dispatcher restarts between
agent-completion and collection (observed 2026-04-20 on auto-m6q15:
decision.json DONE with scores 5/5/5, yet dispatch_runs.status FAILED).

New contract: inspect <output_dir>/decision.json before failing.
  - file DONE → run collection path (commit/merge/close/record + librarian)
  - file FAILED → record as real failure with agent's own `reason`
  - no file + no container → legitimate orphan, mark FAILED as today

FAIL-REASON on master: agents.dispatcher has no function that implements
this contract. Tests either call a missing symbol (AttributeError on
hasattr probe) or assert post-state that today's reconcile does not
produce.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .conftest import (
    fetch_dispatch_row,
    insert_dispatch_run,
    write_decision_json,
)


class TestReconcileDecisionJson:
    """Defect 1 — reconcile honors decision.json."""

    def test_reconcile_rescues_completed_run(self, cleanup_env):
        """decision.json DONE + no container → collect path, not orphaned."""
        dispatcher = cleanup_env["dispatcher"]
        dispatch_db = cleanup_env["dispatch_db"]
        agent_runs = cleanup_env["agent_runs"]

        run_id = "auto-rescue-001-20260420-100000"
        bead_id = "auto-rescue-001"
        output_dir = agent_runs / run_id

        insert_dispatch_run(
            dispatch_db, run_id=run_id, bead_id=bead_id,
            status="RUNNING",
            output_dir=str(output_dir),
            container_name="agent-auto-rescue-001-DEAD",
        )
        write_decision_json(output_dir, status="DONE", reason="work done")

        assert hasattr(dispatcher, "reconcile_orphaned_runs"), (
            "agents.dispatcher.reconcile_orphaned_runs missing — Defect 1 "
            "not yet implemented. Expected callable that inspects "
            "decision.json before marking RUNNING rows as FAILED."
        )

        # Mock docker query — return no containers alive
        with patch.object(dispatcher, "_running_agent_containers",
                          return_value=set(), create=True):
            # Mock the collection pipeline — we don't want the test to
            # actually git-commit, but we want to assert it was invoked.
            collect_mock = MagicMock()
            with patch.object(dispatcher, "_collect_completed_run",
                              collect_mock, create=True):
                dispatcher.reconcile_orphaned_runs()

        # Assert: collect path was invoked with the completed run
        assert collect_mock.call_count == 1, (
            f"Expected _collect_completed_run to fire for completed run; "
            f"got {collect_mock.call_count} calls. Reconcile did not route "
            "the decision.json DONE case through the collection pipeline."
        )
        # The first positional or kwarg should reference the run's id / bead
        call_args = collect_mock.call_args
        call_blob = str(call_args)
        assert run_id in call_blob or bead_id in call_blob, (
            f"_collect_completed_run called but not for the rescued run; "
            f"args={call_args!r}"
        )

        # Row MUST NOT be marked orphaned
        row = fetch_dispatch_row(dispatch_db, run_id)
        assert row is not None
        assert (row.get("reason") or "") != "orphaned: no container at startup", (
            f"Completed run marked orphaned despite decision.json DONE. "
            f"row={row!r}"
        )

    def test_reconcile_marks_real_failure_from_decision_json(self, cleanup_env):
        """decision.json FAILED → row records agent's reason, not 'orphaned'."""
        dispatcher = cleanup_env["dispatcher"]
        dispatch_db = cleanup_env["dispatch_db"]
        agent_runs = cleanup_env["agent_runs"]

        run_id = "auto-fail-002-20260420-100000"
        bead_id = "auto-fail-002"
        output_dir = agent_runs / run_id

        insert_dispatch_run(
            dispatch_db, run_id=run_id, bead_id=bead_id,
            status="RUNNING",
            output_dir=str(output_dir),
            container_name="agent-auto-fail-002-DEAD",
        )
        agent_reason = "Tests 3/13 fail under xdist — unable to reconcile class state"
        write_decision_json(output_dir, status="FAILED", reason=agent_reason)

        assert hasattr(dispatcher, "reconcile_orphaned_runs"), (
            "reconcile_orphaned_runs missing."
        )

        with patch.object(dispatcher, "_running_agent_containers",
                          return_value=set(), create=True), \
             patch.object(dispatcher, "_collect_completed_run",
                          MagicMock(), create=True):
            dispatcher.reconcile_orphaned_runs()

        row = fetch_dispatch_row(dispatch_db, run_id)
        assert row is not None
        assert row.get("status") == "FAILED", f"status={row.get('status')!r}"
        # The agent's own reason must be preserved — NOT clobbered to "orphaned"
        reason = row.get("reason") or ""
        assert agent_reason in reason, (
            f"Reason lost agent-side detail — got {reason!r}, expected to "
            f"contain {agent_reason!r}. Reconcile must honour decision.json "
            "status=FAILED and record the agent's reason verbatim."
        )
        assert "orphaned" not in reason, (
            f"Reason marked 'orphaned' despite decision.json existing with "
            f"a real failure reason. reason={reason!r}"
        )

    def test_reconcile_marks_legitimate_orphan(self, cleanup_env):
        """No decision.json + no container → legitimate orphan, FAILED.

        Regression guard — Defect 1 fix must NOT over-reach. A run with no
        decision.json and no container is genuinely dead; it should still
        be marked FAILED with 'orphaned' reason as today's behavior.
        """
        dispatcher = cleanup_env["dispatcher"]
        dispatch_db = cleanup_env["dispatch_db"]
        agent_runs = cleanup_env["agent_runs"]

        run_id = "auto-orphan-003-20260420-100000"
        bead_id = "auto-orphan-003"
        output_dir = agent_runs / run_id
        output_dir.mkdir(parents=True)  # Dir exists but NO decision.json

        insert_dispatch_run(
            dispatch_db, run_id=run_id, bead_id=bead_id,
            status="RUNNING",
            output_dir=str(output_dir),
            container_name="agent-auto-orphan-003-DEAD",
        )

        assert hasattr(dispatcher, "reconcile_orphaned_runs"), (
            "reconcile_orphaned_runs missing."
        )

        with patch.object(dispatcher, "_running_agent_containers",
                          return_value=set(), create=True), \
             patch.object(dispatcher, "_collect_completed_run",
                          MagicMock(), create=True) as collect_mock:
            dispatcher.reconcile_orphaned_runs()

        assert collect_mock.call_count == 0, (
            "_collect_completed_run fired for a genuine orphan (no "
            "decision.json) — reconcile must not attempt collection "
            "when there's nothing to collect."
        )

        row = fetch_dispatch_row(dispatch_db, run_id)
        assert row is not None
        assert row.get("status") == "FAILED"
        reason = row.get("reason") or ""
        assert "orphan" in reason.lower() or "no container" in reason.lower(), (
            f"Legitimate orphan lost its diagnostic reason; got {reason!r}"
        )
