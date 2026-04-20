"""auto-yaw58 test 2 — the seam: monitor broadcast + tail response agree.

The canonical seam test. Plant a dispatch row with distinct tmux_name
and session_uuid, register via HTTP, capture the first session:messages
broadcast after a JSONL write, fetch the tail response. Assert both
carry the SAME session_id — without pre-specifying which value either
should pick. If they disagree, the overlay's SSE filter drops broadcasts.

Differential assertion: the test doesn't care whether the right answer
is tmux_name or session_uuid. It cares that both ends of the pipe agree.
That's what catches the real-vs-mock divergence: mock fixtures that
auto-populate session_id trivially satisfy agreement by construction;
real code paths derive the value independently and may disagree.

FAIL-REASON on master: broadcast uses tmux_name; tail response returns
session_uuid. tmux_name != session_uuid by test construction → assertion
fails with the two observed values.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from .conftest import insert_raw_dispatch_row, append_jsonl

pytest.importorskip("pytest_asyncio")


class TestSeamAgreement:
    """#2 — monitor broadcast session_id == tail response session_id."""

    @pytest.mark.asyncio
    async def test_monitor_broadcast_session_id_matches_tail_response(self, seam_env):
        srv = seam_env["server"]
        dashboard_db = seam_env["dashboard_db"]
        agent_runs = seam_env["agent_runs"]
        smmod = seam_env["session_monitor"]

        tmux_name = "auto-seam2-0420-100002"
        session_uuid = "33333333-4444-5555-6666-777777777777"
        assert tmux_name != session_uuid, "test invariant"

        sess_dir = agent_runs / tmux_name / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / f"{session_uuid}.jsonl"
        append_jsonl(jsonl, "seed")

        insert_raw_dispatch_row(
            dashboard_db,
            tmux_name=tmux_name,
            session_uuid=session_uuid,
            jsonl_path=str(jsonl),
            bead_id="auto-seam2",
        )

        from starlette.testclient import TestClient
        with TestClient(srv.app) as client:
            # The TestClient lifespan started the monitor. Register via HTTP so
            # the watch is installed on the real jsonl path.
            mon = srv.session_monitor
            with patch.object(
                smmod.SessionMonitor, "_check_tmux",
                staticmethod(lambda name: True),
            ):
                reg = client.post("/api/monitor/register", json={
                    "tmux_name": tmux_name,
                    "type": "dispatch",
                    "jsonl_path": str(jsonl),
                    "bead_id": "auto-seam2",
                    "project": "autonomy",
                })
                assert reg.status_code == 200, reg.text

                # What does the tail endpoint say the session_id is?
                tail = client.get(f"/api/dispatch/tail/{tmux_name}").json()
                tail_sid = tail.get("session_id")
                assert tail_sid in (tmux_name, session_uuid), (
                    f"tail returned unexpected session_id={tail_sid!r}"
                )

                # Subscribe to real event bus for session:messages BEFORE the write
                bus = srv.event_bus
                q = bus.subscribe()

                # Drain any registry-broadcast noise from the register
                await asyncio.sleep(0.3)
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                # Write a new entry — monitor's real inotify + tailer pipeline
                append_jsonl(jsonl, "post-register entry")

                # Capture the first session:messages broadcast within 5s
                deadline = asyncio.get_event_loop().time() + 5.0
                broadcast_sid: str | None = None
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        topic, data, _seq = await asyncio.wait_for(
                            q.get(), timeout=0.5,
                        )
                    except asyncio.TimeoutError:
                        continue
                    if topic == "session:messages":
                        broadcast_sid = data.get("session_id")
                        break

                assert broadcast_sid is not None, (
                    "No session:messages broadcast arrived within 5s after "
                    "JSONL write. Either the register endpoint didn't install "
                    "the inotify watch, or the monitor isn't broadcasting for "
                    "dispatch type. Either way, the seam can't be checked."
                )

                # THE SEAM ASSERTION
                assert broadcast_sid == tail_sid, (
                    f"Seam mismatch: /api/dispatch/tail returned "
                    f"session_id={tail_sid!r} but monitor broadcasts with "
                    f"session_id={broadcast_sid!r}. Planted values were "
                    f"tmux_name={tmux_name!r}, session_uuid={session_uuid!r}. "
                    "The overlay's SSE filter compares data.session_id to the "
                    "configured sessionKey (which comes from the tail "
                    "response). When the broadcast's session_id doesn't match, "
                    "every event is dropped. Fix: both endpoints must agree; "
                    "prefer tmux_name since the monitor already broadcasts that."
                )

                # Final anchor: the value must be tmux_name (not session_uuid
                # and not anything else). This preserves the direction of
                # the fix — unify ON tmux_name, not the other way.
                assert broadcast_sid == tmux_name, (
                    f"Both ends agree (session_id={broadcast_sid!r}), but they "
                    f"agreed on the session_uuid rather than the tmux_name. "
                    "Monitor broadcast was re-keyed incorrectly. Broadcast "
                    "and tail response should both carry tmux_name — that's "
                    "the surface the other call sites (server-side registry, "
                    "dashboard_db.get_session, SSE consumers) are built on."
                )
