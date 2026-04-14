"""
L2.A contract tests for POST /api/session/resume and enriched recent_sessions.

Tests:
- POST /api/session/resume with valid source_id → 200 + tmux_name
- POST /api/session/resume with missing JSONL → 404
- POST /api/session/resume with non-session source → 400
- GET /api/dao/recent_sessions includes session_uuid and resumable fields
"""
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
        assert data["tmux_name"].startswith("resume-")
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
        assert data["tmux_name"].startswith("resume-")

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

    def test_resume_label_contains_uuid_prefix(self, test_client, resume_env):
        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        data = resp.json()
        assert "abc123" in data["label"]


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
        assert data["tmux_name"].startswith("resume-")

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
