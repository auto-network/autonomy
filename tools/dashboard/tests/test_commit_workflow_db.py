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


def test_idempotency_table_created_with_unique_key_and_indexes(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.init_db(path)

    conn = db._get_conn(path)
    try:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(commit_workflow_idempotency)").fetchall()
        }
        assert cols == {
            "idempotency_id",
            "actor_type",
            "actor_id",
            "scope_key",
            "operation",
            "workflow_id",
            "idempotency_key_hash",
            "request_fingerprint",
            "status",
            "response_json",
            "event_ids_json",
            "side_effect_ref",
            "created_at",
            "updated_at",
            "expires_at",
        }

        schema_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commit_workflow_idempotency'"
        ).fetchone()["sql"]
        assert "status IN ('in_flight', 'completed', 'failed_retryable', 'failed_terminal')" in schema_sql

        index_names = {
            row["name"]
            for row in conn.execute("PRAGMA index_list('commit_workflow_idempotency')").fetchall()
        }
        assert "idx_cwi_workflow" in index_names
        assert "idx_cwi_expiry" in index_names

        unique_index = next(
            row["name"]
            for row in conn.execute("PRAGMA index_list('commit_workflow_idempotency')").fetchall()
            if row["unique"]
        )
        unique_cols = [
            row["name"]
            for row in conn.execute(f"PRAGMA index_info('{unique_index}')").fetchall()
        ]
        assert unique_cols == [
            "actor_type",
            "actor_id",
            "scope_key",
            "operation",
            "idempotency_key_hash",
        ]

        conn.execute(
            """
            INSERT INTO commit_workflow_idempotency (
                idempotency_id, actor_type, actor_id, scope_key, operation,
                workflow_id, idempotency_key_hash, request_fingerprint, status,
                response_json, event_ids_json, side_effect_ref,
                created_at, updated_at, expires_at
            ) VALUES (
                'idempo-1', 'agent_session', 'actor-1', 'repo:pre-workflow',
                'propose', NULL, 'hash-1', 'fingerprint-1', 'in_flight',
                NULL, '[]', NULL, 1.0, 1.0, 8.0
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO commit_workflow_idempotency (
                    idempotency_id, actor_type, actor_id, scope_key, operation,
                    workflow_id, idempotency_key_hash, request_fingerprint,
                    status, response_json, event_ids_json, side_effect_ref,
                    created_at, updated_at, expires_at
                ) VALUES (
                    'idempo-2', 'agent_session', 'actor-1', 'repo:pre-workflow',
                    'propose', NULL, 'hash-1', 'fingerprint-2', 'in_flight',
                    NULL, '[]', NULL, 2.0, 2.0, 9.0
                )
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO commit_workflow_idempotency (
                    idempotency_id, actor_type, actor_id, scope_key, operation,
                    workflow_id, idempotency_key_hash, request_fingerprint,
                    status, response_json, event_ids_json, side_effect_ref,
                    created_at, updated_at, expires_at
                ) VALUES (
                    'idempo-3', 'agent_session', 'actor-2', 'repo:pre-workflow',
                    'propose', NULL, 'hash-2', 'fingerprint-3', 'not-a-status',
                    NULL, '[]', NULL, 3.0, 3.0, 10.0
                )
                """
            )
    finally:
        conn.close()


def test_g8_idempotency_key_hash_stored_not_raw_key():
    digest = db.hash_idempotency_key("commit_workflow", "raw-key-123")
    assert digest != "raw-key-123"
    assert len(digest) == 64
    assert all(ch in "0123456789abcdef" for ch in digest)


def test_g8_request_fingerprint_excludes_tokens_and_correlation_ids():
    base = {
        "scope": {"repo_slug": "repo-slug", "workspace_id": "ws-1"},
        "message": {"subject": "subject", "body": "body"},
        "authorization": "Bearer token-a",
        "correlation_id": "corr-a",
        "idempotency_key": "raw-idem-a",
    }
    reordered = {
        "message": {"body": "body", "subject": "subject"},
        "scope": {"workspace_id": "ws-1", "repo_slug": "repo-slug"},
        "authorization": "Bearer token-b",
        "correlation_id": "corr-b",
        "idempotency_key": "raw-idem-b",
    }
    changed = {
        "scope": {"repo_slug": "repo-slug", "workspace_id": "ws-2"},
        "message": {"subject": "subject", "body": "body"},
        "authorization": "Bearer token-c",
        "correlation_id": "corr-c",
    }

    fp1 = db.request_fingerprint("propose", base)
    fp2 = db.request_fingerprint("propose", reordered)
    fp3 = db.request_fingerprint("propose", changed)

    assert fp1 == fp2
    assert fp1 != fp3


def test_scope_key_forms():
    assert db.scope_key("repo-slug", "workflow-1") == "repo-slug:workflow-1"
    assert db.scope_key("repo-slug", "pre-workflow") == "repo-slug:pre-workflow"


def test_idempotency_lifecycle_reserves_replays_and_conflicts(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        request_fields = {
            "scope": {"repo_slug": "repo-slug", "workspace_id": "ws-1"},
            "message": {"subject": "subject", "body": "body"},
            "authorization": "Bearer token-a",
            "correlation_id": "corr-a",
        }
        scope = db.scope_key("repo-slug", "pre-workflow")
        reserved = db.reserve_idempotency(
            conn,
            actor_type="agent_session",
            actor_id="actor-1",
            scope_key=scope,
            operation="propose",
            raw_idempotency_key="raw-idem-1",
            request_fields=request_fields,
            workflow_id=None,
            now=100.0,
        )
        assert reserved["status"] == "in_flight"
        assert reserved["expires_at"] - reserved["created_at"] >= 7 * 24 * 60 * 60

        in_flight = db.lookup_idempotency(
            conn,
            actor_type="agent_session",
            actor_id="actor-1",
            scope_key=scope,
            operation="propose",
            raw_idempotency_key="raw-idem-1",
            request_fields=request_fields,
            now=101.0,
        )
        assert in_flight["kind"] == "in_flight"

        db.finalize_idempotency(
            conn,
            idempotency_id=reserved["idempotency_id"],
            status="completed",
            response_json={"ok": True},
            event_ids=["evt-1"],
            side_effect_ref="provider://ref",
            updated_at=102.0,
        )

        replay = db.lookup_idempotency(
            conn,
            actor_type="agent_session",
            actor_id="actor-1",
            scope_key=scope,
            operation="propose",
            raw_idempotency_key="raw-idem-1",
            request_fields=request_fields,
            now=103.0,
        )
        assert replay["kind"] == "completed_replay"
        assert replay["response_json"] == {"ok": True}
        assert replay["event_ids"] == ["evt-1"]
        assert replay["side_effect_ref"] == "provider://ref"

        conflict = db.lookup_idempotency(
            conn,
            actor_type="agent_session",
            actor_id="actor-1",
            scope_key=scope,
            operation="propose",
            raw_idempotency_key="raw-idem-1",
            request_fields={
                "scope": {"repo_slug": "repo-slug", "workspace_id": "ws-2"},
                "message": {"subject": "subject", "body": "body"},
            },
            now=104.0,
        )
        assert conflict["kind"] == "conflict"
    finally:
        conn.close()


def test_reserve_after_expiry_reuses_key(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        key_hash = db.hash_idempotency_key("commit_workflow", "raw-idem-expired")
        conn.execute(
            """
            INSERT INTO commit_workflow_idempotency (
                idempotency_id, actor_type, actor_id, scope_key, operation,
                workflow_id, idempotency_key_hash, request_fingerprint, status,
                response_json, event_ids_json, side_effect_ref,
                created_at, updated_at, expires_at
            ) VALUES (
                'expired-row', 'agent_session', 'actor-1', 'repo-slug:pre-workflow',
                'propose', NULL, ?, 'fingerprint-old', 'completed',
                '{}', '[]', NULL, 1.0, 1.0, 5.0
            )
            """,
            (key_hash,),
        )

        reserved = db.reserve_idempotency(
            conn,
            actor_type="agent_session",
            actor_id="actor-1",
            scope_key=db.scope_key("repo-slug", "pre-workflow"),
            operation="propose",
            raw_idempotency_key="raw-idem-expired",
            request_fields={"scope": {"repo_slug": "repo-slug"}},
            workflow_id=None,
            now=10.0,
            idempotency_id="fresh-row",
            namespace="commit_workflow",
        )

        rows = conn.execute(
            """
            SELECT idempotency_id, status, expires_at
            FROM commit_workflow_idempotency
            WHERE actor_type = 'agent_session'
              AND actor_id = 'actor-1'
              AND scope_key = 'repo-slug:pre-workflow'
              AND operation = 'propose'
              AND idempotency_key_hash = ?
            """,
            (key_hash,),
        ).fetchall()

        assert reserved["idempotency_id"] == "fresh-row"
        assert len(rows) == 1
        assert rows[0]["idempotency_id"] == "fresh-row"
        assert rows[0]["status"] == "in_flight"
        assert rows[0]["expires_at"] > 10.0
    finally:
        conn.close()


def test_idempotency_retention_windows(tmp_path):
    path = tmp_path / "commit_workflow.db"
    db.init_db(path)
    conn = db._get_conn(path)
    try:
        workflow = db.reserve_idempotency(
            conn,
            actor_type="agent_session",
            actor_id="actor-1",
            scope_key=db.scope_key("repo-slug", "workflow-1"),
            operation="propose",
            raw_idempotency_key="raw-idem-1",
            request_fields={"scope": {"repo_slug": "repo-slug"}},
            workflow_id="workflow-1",
            now=50.0,
        )
        publish = db.reserve_idempotency(
            conn,
            actor_type="agent_session",
            actor_id="actor-1",
            scope_key=db.scope_key("repo-slug", "workflow-1"),
            operation="publish",
            raw_idempotency_key="raw-idem-2",
            request_fields={"scope": {"repo_slug": "repo-slug"}},
            workflow_id="workflow-1",
            now=60.0,
        )
        assert workflow["expires_at"] - workflow["created_at"] >= 7 * 24 * 60 * 60
        assert publish["expires_at"] - publish["created_at"] >= 30 * 24 * 60 * 60
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
