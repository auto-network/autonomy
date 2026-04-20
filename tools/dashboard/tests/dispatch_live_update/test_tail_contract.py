"""auto-yaw58 tests 1 + 3 — tail response session_id contract.

Both /api/dispatch/tail/{runDir} and /api/session/{proj}/{id}/tail must
return session_id = tmux_name for dispatch rows. Today they resolve to
session_uuid via the DAO, which mismatches the monitor's broadcast
session_id (tmux_name) and silently drops every SSE event at the
overlay's subscription filter.

FAIL-REASON on master: tail response's session_id equals session_uuid
(the UUID), not tmux_name. Assertion explicitly distinguishes the two
values and asserts the response carries tmux_name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import insert_raw_dispatch_row, append_jsonl


class TestDispatchTailResponseSessionId:
    """#1 — /api/dispatch/tail/{runDir} must return session_id = tmux_name."""

    def test_dispatch_tail_response_session_id_is_tmux_name(self, seam_env):
        srv = seam_env["server"]
        dashboard_db = seam_env["dashboard_db"]
        agent_runs = seam_env["agent_runs"]

        run_dir = "auto-seam-0420-100000"
        tmux_name = run_dir  # dispatcher convention: tmux_name = run_dir basename
        session_uuid = "11111111-2222-3333-4444-555555555555"
        assert tmux_name != session_uuid

        sess_dir = agent_runs / run_dir / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / f"{session_uuid}.jsonl"
        append_jsonl(jsonl, "seed")

        insert_raw_dispatch_row(
            dashboard_db,
            tmux_name=tmux_name,
            session_uuid=session_uuid,
            jsonl_path=str(jsonl),
            bead_id="auto-seam",
        )

        from starlette.testclient import TestClient
        with TestClient(srv.app) as client:
            resp = client.get(f"/api/dispatch/tail/{run_dir}")
            assert resp.status_code == 200, (
                f"GET /api/dispatch/tail/{run_dir} returned {resp.status_code}: "
                f"{resp.text[:200]!r}"
            )
            body = resp.json()
            returned_sid = body.get("session_id")

            assert returned_sid in (tmux_name, session_uuid), (
                f"tail returned unrecognized session_id={returned_sid!r}; "
                f"expected either {tmux_name!r} (tmux_name) or "
                f"{session_uuid!r} (session_uuid). A third value means a "
                "third naming convention we didn't plant."
            )
            assert returned_sid == tmux_name, (
                f"Dispatch tail response session_id={returned_sid!r} is the "
                f"session_uuid, not the tmux_name. This is the seam bug: the "
                f"monitor broadcasts session:messages with session_id="
                f"{tmux_name!r}, but the overlay subscribes on "
                f"{returned_sid!r} (what this response told it), so every "
                "broadcast is filtered out. Handler must return "
                "session_id = tmux_name for dispatch rows."
            )


class TestUnifiedSessionTailSessionId:
    """#3 — /api/session/{proj}/{id}/tail must also return session_id = tmux_name
    when resolving a dispatch session by its session_uuid.
    """

    def test_unified_session_tail_dispatch_response_session_id(self, seam_env):
        srv = seam_env["server"]
        dashboard_db = seam_env["dashboard_db"]
        agent_runs = seam_env["agent_runs"]

        tmux_name = "auto-unified-0420-100001"
        session_uuid = "22222222-3333-4444-5555-666666666666"
        assert tmux_name != session_uuid

        sess_dir = agent_runs / tmux_name / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / f"{session_uuid}.jsonl"
        append_jsonl(jsonl, "seed")

        insert_raw_dispatch_row(
            dashboard_db,
            tmux_name=tmux_name,
            session_uuid=session_uuid,
            jsonl_path=str(jsonl),
        )

        from starlette.testclient import TestClient
        with TestClient(srv.app) as client:
            # Hit the unified tail by session_uuid (the Recent-card path)
            resp = client.get(f"/api/session/autonomy/{session_uuid}/tail")
            assert resp.status_code == 200, (
                f"GET /api/session/autonomy/{session_uuid}/tail returned "
                f"{resp.status_code}: {resp.text[:200]!r}"
            )
            body = resp.json()
            # Do NOT fall back to tmux_session here — the overlay's SSE
            # filter specifically reads data.session_id, not tmux_session.
            # If session_id carries a different value than tmux_session,
            # the client still drops broadcasts.
            returned_sid = body.get("session_id")

            assert returned_sid == tmux_name, (
                f"Unified tail response carries session_id={returned_sid!r} "
                f"for a dispatch row; expected tmux_name={tmux_name!r}. "
                "Same seam as /api/dispatch/tail — when an overlay or "
                "Recent-card viewer subscribes on this value, broadcasts "
                "keyed on tmux_name get dropped at the SSE filter."
            )
