"""
L2.A contract tests for POST /api/session/resume and enriched recent_sessions.

Tests:
- POST /api/session/resume with valid source_id → 200 + tmux_name
- POST /api/session/resume with missing JSONL → 404
- POST /api/session/resume with non-session source → 400
- POST /api/session/resume with already-active session → 409
- GET /api/dao/recent_sessions includes session_uuid and resumable fields
- Session identity preservation: dead session → reuse original tmux_name + label
- History backfill: resumed session passes JSONL path for full history
- Re-resume: a session that died after resume can be resumed again
- Workspace primer is rendered into the run_dir on resume
"""
from pathlib import Path

import pytest


class TestResumeWithSourceId:
    """POST /api/session/resume with source_id."""

    def test_valid_container_source_returns_200(self, test_client, resume_env):
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "tmux_name" in data
        assert data["type"] == "container"
        assert "label" in data

    def test_valid_host_source_returns_200(self, test_client, resume_env):
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["host_source_id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "host"

    def test_missing_jsonl_returns_404(self, test_client, resume_env):
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["missing_source_id"]},
        )
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    def test_non_session_source_returns_400(self, test_client, resume_env):
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["non_session_source_id"]},
        )
        assert resp.status_code == 400
        assert "not a session" in resp.json()["error"].lower()

    def test_unknown_source_returns_404(self, test_client):
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": "src-does-not-exist"},
        )
        assert resp.status_code == 404

    def test_resume_label_from_graph_title(self, test_client, resume_env):
        """Label should come from graph source title when no dead session exists."""
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        data = resp.json()
        assert data["label"] == "Container session alpha"


class TestResumeWithDirectParams:
    """POST /api/session/resume with session_uuid + file_path."""

    def test_direct_params_returns_200(self, test_client, resume_env):
        resp = test_client.post(
            "/api/session/resume",
            json={
                "session_uuid": "abc123-def456",
                "file_path": resume_env["jsonl_file"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "tmux_name" in data

    def test_missing_session_uuid_returns_400(self, test_client, resume_env):
        resp = test_client.post(
            "/api/session/resume",
            json={"file_path": resume_env["jsonl_file"]},
        )
        assert resp.status_code == 400
        assert "session_uuid" in resp.json()["error"]

    def test_missing_file_path_returns_400(self, test_client):
        resp = test_client.post(
            "/api/session/resume",
            json={"session_uuid": "some-uuid"},
        )
        assert resp.status_code == 400
        assert "file_path" in resp.json()["error"]

    def test_invalid_json_returns_400(self, test_client):
        resp = test_client.post(
            "/api/session/resume",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400


class TestSessionIdentityPreservation:
    """Resumed session reuses the ORIGINAL tmux_name + label from dashboard.db."""

    def test_reuses_original_tmux_name(self, test_client, resume_env):
        """When a dead session exists in dashboard.db, resume reuses its tmux_name."""
        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Passkey auth design",
            "role": "researcher",
            "topics": '["auth", "passkeys"]',
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tmux_name"] == "auto-0326-142603"

    def test_preserves_original_label(self, test_client, resume_env):
        """Resumed session returns the original label, not 'Resumed: ...'."""
        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Passkey auth design",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        data = resp.json()
        assert data["label"] == "Passkey auth design"

    def test_calls_revive_session(self, test_client, resume_env):
        """When reusing a dead session, revive_session is called with file_offset=0."""
        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Test session",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        assert len(test_client._revived) == 1
        assert test_client._revived[0]["tmux_name"] == "auto-0326-142603"
        assert test_client._revived[0]["file_offset"] == 0

    def test_no_dead_session_generates_new_name(self, test_client, resume_env):
        """Without a dead session, a new name is generated (not reused)."""
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        # No dead session → should NOT be "auto-0326-..." but should be some generated name
        assert data["tmux_name"]  # just ensure it's non-empty
        assert len(test_client._revived) == 0  # revive_session NOT called

    def test_lookup_by_file_path(self, test_client, resume_env):
        """Dead session can be found by file_path when session_uuid doesn't match."""
        test_client._dead_sessions["other-uuid"] = {
            "tmux_name": "auto-0328-100000",
            "is_live": 0,
            "label": "Found by path",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "other-uuid",
            "type": "container",
            "project": "autonomy",
        }
        # Use direct params with the matching file_path but different uuid
        resp = test_client.post(
            "/api/session/resume",
            json={
                "session_uuid": "abc123-def456",
                "file_path": resume_env["jsonl_file"],
            },
        )
        assert resp.status_code == 200
        # Should find by file_path fallback
        data = resp.json()
        assert data["tmux_name"] == "auto-0328-100000"


class TestHistoryBackfill:
    """Resumed session provides JSONL path for full conversation history."""

    def test_revived_session_uses_register_revived(self, test_client, resume_env):
        """When a dead session is revived, register_revived is called (not register)."""
        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Test",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert len(test_client._monitor_calls["register_revived"]) == 1
        assert len(test_client._monitor_calls["register"]) == 0
        call = test_client._monitor_calls["register_revived"][0]
        assert call["tmux_name"] == "auto-0326-142603"
        assert str(call["jsonl_path"]) == resume_env["jsonl_file"]

    def test_new_session_passes_jsonl_to_register(self, test_client, resume_env):
        """When no dead session exists, register is called WITH the JSONL path."""
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        assert len(test_client._monitor_calls["register"]) == 1
        call = test_client._monitor_calls["register"][0]
        assert str(call["jsonl_path"]) == resume_env["jsonl_file"]
        assert call["session_uuid"] == "abc123-def456"

    def test_host_session_passes_jsonl_to_register(self, test_client, resume_env):
        """Host sessions also pass JSONL path for backfill."""
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["host_source_id"]},
        )
        assert resp.status_code == 200
        assert len(test_client._monitor_calls["register"]) == 1
        call = test_client._monitor_calls["register"][0]
        assert str(call["jsonl_path"]) == resume_env["host_jsonl"]


class TestReResumeAfterDeath:
    """A session that was resumed and then died can be resumed again."""

    def test_can_resume_previously_resumed_session(self, test_client, resume_env):
        """Session with original name can be re-resumed after dying again."""
        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Already resumed once",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tmux_name"] == "auto-0326-142603"
        assert data["label"] == "Already resumed once"
        # revive_session resets file_offset for full re-backfill
        assert test_client._revived[0]["file_offset"] == 0


class TestActiveSessionGuard:
    """POST /api/session/resume rejects already-active sessions with 409."""

    def test_active_session_by_uuid_returns_409(self, test_client, resume_env):
        """Resuming a session whose session_uuid is live returns 409."""
        test_client._live_sessions["abc123-def456"] = {
            "tmux_name": "auto-t1",
            "is_live": 1,
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 409
        assert "already active" in resp.json()["error"].lower()
        assert "auto-t1" in resp.json()["error"]

    def test_active_session_by_direct_params_returns_409(self, test_client, resume_env):
        """Resuming via session_uuid + file_path also triggers the guard."""
        test_client._live_sessions["abc123-def456"] = {
            "tmux_name": "auto-t1",
            "is_live": 1,
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={
                "session_uuid": "abc123-def456",
                "file_path": resume_env["jsonl_file"],
            },
        )
        assert resp.status_code == 409
        assert "auto-t1" in resp.json()["error"]

    def test_active_session_by_file_path_returns_409(self, test_client, resume_env):
        """Guard also matches by file_path when session_uuid differs."""
        test_client._live_sessions["other-live-uuid"] = {
            "tmux_name": "auto-running",
            "is_live": 1,
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "other-live-uuid",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={
                "session_uuid": "abc123-def456",
                "file_path": resume_env["jsonl_file"],
            },
        )
        assert resp.status_code == 409
        assert "auto-running" in resp.json()["error"]

    def test_dead_session_still_resumes_ok(self, test_client, resume_env):
        """A dead session (not in _live_sessions) still resumes normally."""
        # No live session set — should succeed
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200


class TestWorkspacePrimerRendering:
    """Resumed workspace sessions render the primer into the run_dir."""

    def test_workspace_resume_writes_primer_to_run_dir(self, test_client, resume_env):
        """Resume of a workspace session should regenerate .claude_md from the
        current workspace config, identical to the create path."""
        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Workspace session",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        run_dir = Path(resume_env["jsonl_file"]).parent.parent.parent
        primer_path = run_dir / ".claude_md"
        assert primer_path.exists(), f"primer not rendered at {primer_path}"
        content = primer_path.read_text()
        assert "Workspace Environment" in content
        assert "autonomy-agent:dashboard" in content


class TestRecentSessionsEnriched:
    """GET /api/dao/recent_sessions includes session_uuid and resumable."""

    def test_has_session_uuid_field(self, test_client):
        resp = test_client.get("/api/dao/recent_sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        for session in data:
            assert "session_uuid" in session

    def test_has_resumable_field(self, test_client):
        data = test_client.get("/api/dao/recent_sessions").json()
        for session in data:
            assert "resumable" in session
            assert isinstance(session["resumable"], bool)

    def test_has_file_path_field(self, test_client):
        data = test_client.get("/api/dao/recent_sessions").json()
        for session in data:
            assert "file_path" in session

    def test_resumable_reflects_file_existence(self, test_client):
        data = test_client.get("/api/dao/recent_sessions").json()
        by_id = {s["id"]: s for s in data}
        # The container session has a real JSONL file
        if "src-container-session" in by_id:
            assert by_id["src-container-session"]["resumable"] is True
        # The missing-jsonl session has no file
        if "src-missing-jsonl" in by_id:
            assert by_id["src-missing-jsonl"]["resumable"] is False
