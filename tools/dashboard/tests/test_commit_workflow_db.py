from __future__ import annotations

import sqlite3

import pytest

from tools.dashboard.dao import commit_workflow_db as db


def _event(tmp_path, *, workflow_id: str, status: str, shas: list[str], event_id: str | None = None) -> None:
    db.append_event(
        event_id=event_id or f"event-{workflow_id}-{status}",
        workflow_id=workflow_id,
        event_type=status,
        status_after=status,
        repo_slug="autonomy",
        commit_shas=shas,
        db_path=tmp_path / "commit_workflow.db",
    )


def _resolve(tmp_path, shas: list[str], *, merged: set[str] | None = None):
    return {
        item.sha: item
        for item in db.resolve_worktree_outstanding(
            repo_slug="autonomy",
            scanned_shas=shas,
            git_merged_shas=merged or set(),
            db_path=tmp_path / "commit_workflow.db",
        )
    }


def test_events_are_append_only_even_with_insert_or_replace(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.init_db(path)
    _event(tmp_path, workflow_id="wf1", status="proposed", shas=["A"], event_id="e1")

    conn = db._get_conn(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT OR REPLACE INTO commit_workflow_events (
                    event_id, workflow_id, event_type, status_after, occurred_at,
                    actor_type, repo_slug
                ) VALUES ('e1', 'wf1', 'status', 'landed', 2.0, 'test', 'autonomy')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE commit_workflow_events SET status_after='landed' WHERE event_id='e1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM commit_workflow_events WHERE event_id='e1'")
    finally:
        conn.close()


def test_resolver_t1_no_double_count_terminal_workflow_suppresses_git(tmp_path):
    _event(tmp_path, workflow_id="wf1", status="landed", shas=["A"])

    result = _resolve(tmp_path, ["A"])

    assert result["A"].outstanding is False
    assert result["A"].source == "workflow:landed"


def test_resolver_t2_no_vanish_without_workflow_evidence(tmp_path):
    _event(tmp_path, workflow_id="wf1", status="landed", shas=["A"])

    result = _resolve(tmp_path, ["A", "B"])

    assert result["A"].outstanding is False
    assert result["B"].outstanding is True
    assert result["B"].source == "git_derivation"


def test_resolver_t3_suppresses_only_after_terminal_event_backfill(tmp_path):
    before = _resolve(tmp_path, ["A", "B"])
    assert before["A"].outstanding is True
    assert before["B"].outstanding is True

    _event(tmp_path, workflow_id="backfill-A", status="landed", shas=["A"])

    after = _resolve(tmp_path, ["A", "B"])
    assert after["A"].outstanding is False
    assert after["A"].source == "workflow:landed"
    assert after["B"].outstanding is True


def test_resolver_t4_reverted_reappears_unless_git_says_back_on_target(tmp_path):
    _event(tmp_path, workflow_id="wf1", status="landed", shas=["A"], event_id="e1")
    _event(tmp_path, workflow_id="wf1", status="reverted", shas=["A"], event_id="e2")

    off_target = _resolve(tmp_path, ["A"])
    back_on_target = _resolve(tmp_path, ["A"], merged={"A"})

    assert off_target["A"].outstanding is True
    assert off_target["A"].source == "workflow:reverted"
    assert back_on_target["A"].outstanding is False


def test_status_only_terminal_event_preserves_prior_commit_mapping(tmp_path):
    _event(tmp_path, workflow_id="wf1", status="proposed", shas=["A"], event_id="e1")
    _event(tmp_path, workflow_id="wf1", status="landed", shas=[], event_id="e2")

    result = _resolve(tmp_path, ["A"])

    assert result["A"].outstanding is False
    assert result["A"].source == "workflow:landed"


def test_resolver_maps_multi_commit_workflow(tmp_path):
    _event(tmp_path, workflow_id="wf1", status="landed", shas=["sha1", "sha2"])

    result = _resolve(tmp_path, ["sha1", "sha2"])

    assert result["sha1"].outstanding is False
    assert result["sha2"].outstanding is False
    assert result["sha1"].workflow_id == "wf1"
    assert result["sha2"].workflow_id == "wf1"


def test_resolver_landed_dominates_stale_duplicate_active(tmp_path):
    _event(tmp_path, workflow_id="wf-landed", status="landed", shas=["A"])
    _event(tmp_path, workflow_id="wf-dup", status="duplicate_active", shas=["A"])

    result = _resolve(tmp_path, ["A"])

    assert result["A"].outstanding is False
    assert result["A"].source == "workflow:landed"


def test_duplicate_active_is_recordable_but_second_canonical_active_is_rejected(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.append_event(
        event_id="e1",
        workflow_id="wf1",
        event_type="proposed",
        status_after="proposed",
        repo_slug="autonomy",
        commit_shas=["A"],
        content_fingerprint="fp",
        db_path=path,
    )
    db.append_event(
        event_id="e2",
        workflow_id="wf-dup",
        event_type="duplicate_active",
        status_after="duplicate_active",
        repo_slug="autonomy",
        commit_shas=["A"],
        content_fingerprint="fp",
        db_path=path,
    )

    with pytest.raises(sqlite3.IntegrityError):
        db.append_event(
            event_id="e3",
            workflow_id="wf2",
            event_type="proposed",
            status_after="proposed",
            repo_slug="autonomy",
            commit_shas=["B"],
            content_fingerprint="fp",
            db_path=path,
        )


def test_status_typo_rejected_and_terminal_is_generated(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.init_db(path)
    with pytest.raises(ValueError):
        db.append_event(
            event_id="e1",
            workflow_id="wf1",
            event_type="bad",
            status_after="lande",
            repo_slug="autonomy",
            commit_shas=["A"],
            db_path=path,
        )

    _event(tmp_path, workflow_id="wf1", status="landed", shas=["A"], event_id="e2")
    conn = db._get_conn(path)
    try:
        row = conn.execute(
            "SELECT terminal FROM commit_workflow_states WHERE workflow_id='wf1'"
        ).fetchone()
        assert row["terminal"] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                """
                INSERT INTO commit_workflow_states (
                    workflow_id, repo_slug, status, terminal, last_event_id,
                    created_at, updated_at
                ) VALUES ('wf2', 'autonomy', 'landed', 0, 'e2', 1.0, 1.0)
                """
            )
    finally:
        conn.close()


def test_projection_can_be_rebuilt_from_events(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.append_event(
        event_id="e1",
        workflow_id="wf1",
        event_type="proposed",
        status_after="proposed",
        repo_slug="autonomy",
        commit_shas=["A"],
        db_path=path,
    )
    db.append_event(
        event_id="e2",
        workflow_id="wf1",
        event_type="landed",
        status_after="landed",
        repo_slug="autonomy",
        commit_shas=["A"],
        db_path=path,
    )
    before = _resolve(tmp_path, ["A"])["A"]

    db.rebuild_projection(path)
    after = _resolve(tmp_path, ["A"])["A"]

    assert before == after
    assert after.outstanding is False
    assert after.source == "workflow:landed"


def test_resolver_read_path_does_not_run_schema_ddl(tmp_path, monkeypatch):
    _event(tmp_path, workflow_id="wf1", status="landed", shas=["A"])
    calls = 0

    def fail_init(_conn):
        nonlocal calls
        calls += 1
        raise AssertionError("read path should not initialize schema")

    monkeypatch.setattr(db, "init_schema_on_connection", fail_init)

    result = _resolve(tmp_path, ["A"])

    assert calls == 0
    assert result["A"].outstanding is False


def test_resolver_missing_table_falls_back_to_git_without_ddl(tmp_path, monkeypatch):
    calls = 0

    def fail_init(_conn):
        nonlocal calls
        calls += 1
        raise AssertionError("read path should not initialize schema")

    monkeypatch.setattr(db, "init_schema_on_connection", fail_init)

    result = _resolve(tmp_path, ["A"])

    assert calls == 0
    assert result["A"].outstanding is True
    assert result["A"].source == "git_derivation"
