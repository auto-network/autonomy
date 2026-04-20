"""auto-opbyh test #5 — END-TO-END UNMOCKED contract.

This is the test the auto-ylj6r Phase 0 contract was missing. Full path:

  1. POST /api/monitor/register (real HTTP via TestClient).
  2. Append an entry to the JSONL file (real filesystem write).
  3. Assert a session:messages SSE broadcast fires for that session_id.

No patching of _check_tmux. No direct in-process register_session() call.
No MockEventBus — uses the real event_bus attached to the running server.

FAIL-REASON on master: /api/monitor/register endpoint does not exist, so
the first POST returns 404. Even if a handler existed that only did a DB
upsert (the auto-ylj6r path), no inotify watch gets installed, so the
subsequent file append produces no SSE broadcast. Both conditions —
endpoint and full side effects — must be satisfied for this test to pass.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("pytest_asyncio")
pytest.importorskip("inotify_simple")
pytest.importorskip("starlette")


@pytest.mark.asyncio
class TestRegisterFiresBroadcast:
    """#5 — Real path: register, write JSONL, session:messages arrives."""

    async def test_register_then_jsonl_write_fires_sse_broadcast(
        self, ipc_env
    ):
        srv = ipc_env["server"]
        tmp_path = ipc_env["tmp_path"]

        sess_dir = tmp_path / "agent-runs" / "auto-e2e-005" / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / "e2e-abcd.jsonl"
        jsonl.write_text("")

        tmux_name = "auto-e2e-005"

        from starlette.testclient import TestClient

        with TestClient(srv.app) as client:
            # Subscribe to the event bus BEFORE the write so we catch the broadcast.
            bus = srv.event_bus
            assert bus is not None, (
                "server module has no event_bus — lifespan startup failed."
            )
            q = bus.subscribe()

            # Register via HTTP
            resp = client.post(
                "/api/monitor/register",
                json={
                    "tmux_name": tmux_name,
                    "type": "dispatch",
                    "jsonl_path": str(jsonl),
                    "bead_id": "auto-e2e",
                    "project": "autonomy",
                    "run_dir": str(sess_dir.parent),
                },
            )
            assert resp.status_code == 200, (
                f"register POST failed: {resp.status_code} body={resp.text[:200]!r}"
            )

            # Drain registry events we don't care about
            await asyncio.sleep(0.2)
            drained = 0
            while not q.empty():
                try:
                    q.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break

            # Write a JSONL entry — real filesystem, real inotify
            entry = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello from e2e test"}],
                },
                "timestamp": "2026-04-20T17:00:00Z",
            }
            with open(jsonl, "a") as f:
                f.write(json.dumps(entry) + "\n")

            # Wait for the broadcast to arrive
            deadline = time.monotonic() + 5.0
            got_messages = False
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    topic, data, _seq = await asyncio.wait_for(
                        q.get(), timeout=max(0.05, remaining)
                    )
                except asyncio.TimeoutError:
                    break
                if topic == "session:messages" and data.get("session_id") == tmux_name:
                    got_messages = True
                    break

            assert got_messages, (
                "No session:messages SSE event arrived within 5s after "
                f"appending to {jsonl}. The register endpoint did not install "
                "an inotify watch that actually triggers broadcasts on write. "
                "This is the auto-ylj6r IPC gap: a DB-only upsert doesn't wire "
                "up the in-process monitor. Handler must call "
                "session_monitor.register_session() in-process."
            )
