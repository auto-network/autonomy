"""auto-opbyh tests #1, #2, #3 — /api/monitor/register + /api/monitor/deregister.

These tests POST to the real dashboard HTTP surface via TestClient and assert
both the DB side-effect AND the in-process monitor side-effects (inotify
watch installed, tail state created). That combination is what auto-ylj6r
missed — direct DB writes satisfied the DB part of the contract without
triggering the in-process monitor at all.

FAIL-REASON on master: /api/monitor/register and /api/monitor/deregister
endpoints do not exist. TestClient returns 404 for the POST. Plus even if
a handler existed that only did a DB insert, the watch-installed assertion
would fail (this is the auto-ylj6r gap encoded as an assertion).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from .conftest import fetch_row

pytest.importorskip("starlette")


@pytest.mark.asyncio
class TestRegisterEndpoint:
    """#1 + #2 — POST /api/monitor/register must create DB row + install watch."""

    async def test_register_endpoint_creates_row_and_watch(
        self, ipc_env    ):
        srv = ipc_env["server"]
        smmod = ipc_env["session_monitor"]
        tmp_path = ipc_env["tmp_path"]
        db_path = ipc_env["db_path"]

        sess_dir = tmp_path / "agent-runs" / "auto-test-001" / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / "abcd.jsonl"
        jsonl.write_text("")

        from starlette.testclient import TestClient

        # TestClient needs to manage the app lifespan so the monitor starts.
        with TestClient(srv.app) as client:
            # Grab the running monitor instance from the server module so we
            # can inspect its in-memory state AFTER the endpoint returns.
            mon = srv.session_monitor
            assert mon is not None, (
                "server module has no running session_monitor instance — "
                "lifespan startup did not initialise it."
            )

            resp = client.post(
                "/api/monitor/register",
                json={
                    "tmux_name": "auto-test-001",
                    "type": "dispatch",
                    "jsonl_path": str(jsonl),
                    "bead_id": "auto-test",
                    "project": "autonomy",
                    "run_dir": str(sess_dir.parent),
                },
            )

            assert resp.status_code == 200, (
                f"POST /api/monitor/register returned {resp.status_code}; "
                f"body={resp.text[:200]!r}. Endpoint is missing on master — "
                "add handler that calls session_monitor.register_session()."
            )

            # DB row exists with type='dispatch'
            row = fetch_row(db_path, "auto-test-001")
            assert row is not None, (
                "register endpoint returned 200 but no DB row — handler "
                "must call session_monitor.register_session() which inserts."
            )
            assert row["type"] == "dispatch", f"type={row['type']!r}"
            assert row["jsonl_path"] == str(jsonl), (
                f"jsonl_path not persisted: {row['jsonl_path']!r}"
            )

            # In-process side effects — the auto-ylj6r gap test.
            # 1. _tail_states dict contains an entry for the tmux_name
            assert "auto-test-001" in mon._tail_states, (
                "register endpoint did not create monitor._tail_states entry "
                "for 'auto-test-001'. Handler must invoke the monitor's "
                "in-process register_session() — NOT just a DB upsert. "
                "This is the auto-ylj6r gap (graph://f4b1bb26-a1)."
            )
            # 2. An inotify watch is installed on the jsonl file.
            #    Either the watch descriptor is set on _TailState, OR the
            #    tmux_name appears in the monitor's _wd_to_session map.
            ts = mon._tail_states["auto-test-001"]
            watch_on_file = (
                getattr(ts, "watch_descriptor", None) is not None
                or "auto-test-001" in set(mon._wd_to_session.values())
            )
            assert watch_on_file, (
                "register endpoint did not install inotify watch for the "
                "JSONL path. Handler invoked but side effects missing."
            )

            # Give the monitor a moment to settle — verify response shape
            body = resp.json()
            assert body.get("ok") is True, f"response body: {body!r}"
            assert body.get("tmux_name") == "auto-test-001", body

    async def test_register_endpoint_idempotent(self, ipc_env):
        srv = ipc_env["server"]
        tmp_path = ipc_env["tmp_path"]
        db_path = ipc_env["db_path"]

        sess_dir = tmp_path / "agent-runs" / "auto-test-002" / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / "efgh.jsonl"
        jsonl.write_text("")

        from starlette.testclient import TestClient

        with TestClient(srv.app) as client:
            body = {
                "tmux_name": "auto-test-002",
                "type": "dispatch",
                "jsonl_path": str(jsonl),
                "bead_id": "auto-test",
                "project": "autonomy",
            }
            r1 = client.post("/api/monitor/register", json=body)
            assert r1.status_code == 200, f"first POST: {r1.status_code} {r1.text[:200]}"

            r2 = client.post("/api/monitor/register", json=body)
            assert r2.status_code == 200, (
                f"second POST (idempotent re-register) returned {r2.status_code} "
                f"body={r2.text[:200]!r}. Handler must accept re-registration "
                "without 400/409."
            )

            # Only ONE row in the DB
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM tmux_sessions WHERE tmux_name=?",
                ("auto-test-002",),
            ).fetchone()
            conn.close()
            assert count == 1, (
                f"Idempotent re-register produced {count} rows for "
                "tmux_name='auto-test-002'; must collapse to 1 via upsert."
            )


@pytest.mark.asyncio
class TestDeregisterEndpoint:
    """#3 — POST /api/monitor/deregister marks is_live=0, preserves row + jsonl_path."""

    async def test_deregister_endpoint_marks_dead_preserves_row(
        self, ipc_env    ):
        srv = ipc_env["server"]
        tmp_path = ipc_env["tmp_path"]
        db_path = ipc_env["db_path"]

        sess_dir = tmp_path / "agent-runs" / "auto-test-003" / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / "xxxx.jsonl"
        jsonl.write_text("")

        from starlette.testclient import TestClient

        with TestClient(srv.app) as client:
            mon = srv.session_monitor
            # Register first
            r1 = client.post(
                "/api/monitor/register",
                json={
                    "tmux_name": "auto-test-003",
                    "type": "dispatch",
                    "jsonl_path": str(jsonl),
                    "bead_id": "auto-test",
                    "project": "autonomy",
                },
            )
            assert r1.status_code == 200, f"register precondition failed: {r1.text[:200]}"

            # Deregister
            r2 = client.post(
                "/api/monitor/deregister",
                json={"tmux_name": "auto-test-003"},
            )
            assert r2.status_code == 200, (
                f"POST /api/monitor/deregister returned {r2.status_code} "
                f"body={r2.text[:200]!r}. Endpoint is missing on master."
            )

            row = fetch_row(db_path, "auto-test-003")
            assert row is not None, (
                "Deregister deleted the row — it must preserve the row "
                "with is_live=0 for history lookups."
            )
            assert row["is_live"] == 0, (
                f"Expected is_live=0 after deregister, got {row['is_live']}"
            )
            assert row["jsonl_path"] == str(jsonl), (
                f"jsonl_path lost during deregister: {row['jsonl_path']!r}"
            )

            # In-process state should be cleaned up
            assert "auto-test-003" not in mon._tail_states, (
                "Deregister did not clean monitor._tail_states entry; "
                "handler must invoke session_monitor.deregister_session()."
            )
