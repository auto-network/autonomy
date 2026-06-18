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
import asyncio
import time
import pytest

from agents.workspace_settings import (
    CAPABILITIES_MOUNT_DIR,
    MaterializedCapability,
    RepoMount,
    WorkspaceV1,
)


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


class TestWorkspaceHarnessPassthrough:
    """Workspace create/resume must honor the workspace harness setting."""

    def test_workspace_create_enqueues_codex_lifecycle_job(
        self, test_client, monkeypatch,
    ):
        from tools.dashboard import server

        enqueued = []
        workspace = WorkspaceV1(
            id="autonomy",
            name="Autonomy Codex",
            description="",
            image="autonomy-agent:dashboard",
            graph_project="autonomy",
            harness="codex",
            repos=(RepoMount(url="git@example.com:autonomy.git", mount="/workspace/repo", writable=True),),
            working_dir="/workspace/repo",
        )

        monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _name: workspace)
        monkeypatch.setattr(server.workspace_settings, "validate_artifacts", lambda _proj: [])
        monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
        monkeypatch.setattr(
            server._SESSION_LIFECYCLE_WORKER,
            "try_enqueue",
            lambda job: not enqueued.append(job),
        )

        resp = test_client.post("/api/session/create", json={"project": "autonomy"})
        assert resp.status_code == 202
        assert resp.json()["pending"] is True
        assert len(enqueued) == 1
        job = enqueued[0]
        assert job.action == "start"
        assert job.config["project_id"] == "autonomy"
        assert job.config["primer_url"] is None
        assert job.config["attempt"] == 1
        assert isinstance(job.config["event_loop"], asyncio.AbstractEventLoop)

        row = server.dashboard_db.get_session(job.tmux_name)
        assert row["startup_state"] == "requesting"
        assert row["harness"] == "codex"

    def test_workspace_create_returns_without_running_prepare_on_request_path(
        self, test_client, monkeypatch,
    ):
        """Workspace create must only enqueue lifecycle work on the request path."""
        from tools.dashboard import server

        workspace = WorkspaceV1(
            id="autonomy",
            name="Autonomy Codex",
            description="",
            image="autonomy-agent:dashboard",
            graph_project="autonomy",
            harness="codex",
            repos=(RepoMount(url="git@example.com:autonomy.git", mount="/workspace/repo", writable=True),),
            working_dir="/workspace/repo",
        )

        monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _name: workspace)
        monkeypatch.setattr(server.workspace_settings, "validate_artifacts", lambda _proj: [])
        monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
        monkeypatch.setattr(server._SESSION_LIFECYCLE_WORKER, "try_enqueue", lambda _job: True)

        def slow_prepare(_proj, _tmux_name, **_kwargs):
            raise AssertionError("prepare_session_mounts must not run on the request path")

        monkeypatch.setattr(server, "prepare_session_mounts", slow_prepare)

        t0 = time.monotonic()
        resp = test_client.post("/api/session/create", json={"project": "autonomy"})
        elapsed = time.monotonic() - t0

        assert resp.status_code == 202
        assert resp.json()["pending"] is True
        assert elapsed < 0.5

    def test_workspace_create_returns_503_when_lifecycle_queue_full(
        self, test_client, monkeypatch,
    ):
        from tools.dashboard import server

        workspace = WorkspaceV1(
            id="autonomy",
            name="Autonomy Codex",
            description="",
            image="autonomy-agent:dashboard",
            graph_project="autonomy",
            harness="codex",
            repos=(RepoMount(url="git@example.com:autonomy.git", mount="/workspace/repo", writable=True),),
            working_dir="/workspace/repo",
        )

        monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _name: workspace)
        monkeypatch.setattr(server.workspace_settings, "validate_artifacts", lambda _proj: [])

        monkeypatch.setattr(server._SESSION_LIFECYCLE_WORKER, "try_enqueue", lambda _job: False)

        resp = test_client.post("/api/session/create", json={"project": "autonomy"})

        assert resp.status_code == 503
        payload = resp.json()
        assert payload["retryable"] is True
        row = server.dashboard_db.get_session(payload["tmux_name"])
        assert row["startup_state"] == "setup_failed"
        assert row["activity_state"] == "failed"
        assert row["is_live"] == 0
        assert "session lifecycle queue is full" in row["lifecycle_detail"]

    def test_workspace_resume_passes_codex_harness_without_refreshing_existing_worktree(
        self, test_client, resume_env, monkeypatch,
    ):
        from tools.dashboard import server

        launch_kwargs = {}
        prep_kwargs = {}
        workspace = WorkspaceV1(
            id="autonomy",
            name="Autonomy Codex",
            description="",
            image="autonomy-agent:dashboard",
            graph_project="autonomy",
            harness="codex",
            repos=(RepoMount(url="git@example.com:autonomy.git", mount="/workspace/repo", writable=True),),
            working_dir="/workspace/repo",
        )

        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Workspace session",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }

        monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _name: workspace)
        monkeypatch.setattr(server.workspace_settings, "validate_artifacts", lambda _proj: [])
        monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
        monkeypatch.setattr(server, "render_workspace_primer", lambda _proj: "primer")

        def fake_prepare(_proj, _tmux_name, **kwargs):
            prep_kwargs.update(kwargs)
            return {}

        def fake_launch_session(**kwargs):
            launch_kwargs.update(kwargs)
            return "docker run codex"

        monkeypatch.setattr(server, "prepare_session_mounts", fake_prepare)
        monkeypatch.setattr(server, "launch_session", fake_launch_session)

        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        assert launch_kwargs["harness"] == "codex"
        assert prep_kwargs["refresh_existing_worktree"] is False


class TestWorkspaceCapabilityPassthrough:
    """Workspace create/resume must preserve resolved capabilities.

    `auto-uqq0i` proved the substrate in isolation but never wired the
    real Dashboard route to forward `proj.capabilities`. Create now
    hands off by project id to the lifecycle worker, which re-reads the
    workspace and forwards capabilities to the launcher.
    """

    @staticmethod
    def _jira_capability() -> MaterializedCapability:
        return MaterializedCapability(
            contract="issue_tracker",
            contract_version=1,
            implementation="autonomy/jira",
            implementation_version=1,
            delivery_mode="mounted_tools",
            package_root="agents/capabilities/jira",
            mount_target=f"{CAPABILITIES_MOUNT_DIR}/autonomy-jira",
            required_env=("JIRA_EMAIL", "JIRA_BASE_URL"),
            required_secret_files=("/run/secrets/jira_token",),
            tool_paths=("agents/capabilities/jira/tools",),
            primer_path="agents/capabilities/jira/primer.md",
        )

    def _workspace_with_capabilities(self) -> WorkspaceV1:
        return WorkspaceV1(
            id="autonomy",
            name="Autonomy Jira",
            description="",
            image="autonomy-agent:dashboard",
            graph_project="autonomy",
            harness="claude",
            repos=(RepoMount(url="git@example.com:autonomy.git", mount="/workspace/repo", writable=True),),
            working_dir="/workspace/repo",
            capabilities=(self._jira_capability(),),
        )

    def test_workspace_create_enqueues_capability_workspace_job(
        self, test_client, monkeypatch,
    ):
        from tools.dashboard import server

        enqueued = []
        workspace = self._workspace_with_capabilities()

        monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _name: workspace)
        monkeypatch.setattr(server.workspace_settings, "validate_artifacts", lambda _proj: [])
        monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
        monkeypatch.setattr(server._SESSION_LIFECYCLE_WORKER, "enqueue", enqueued.append)

        resp = test_client.post("/api/session/create", json={"project": "autonomy"})
        assert resp.status_code == 202
        assert enqueued[0].config["project_id"] == workspace.id

    def test_workspace_resume_passes_capabilities_to_launch_session(
        self, test_client, resume_env, monkeypatch,
    ):
        from tools.dashboard import server

        launch_kwargs: dict = {}
        workspace = self._workspace_with_capabilities()

        test_client._dead_sessions["abc123-def456"] = {
            "tmux_name": "auto-0326-142603",
            "is_live": 0,
            "label": "Workspace session",
            "jsonl_path": resume_env["jsonl_file"],
            "session_uuid": "abc123-def456",
            "type": "container",
            "project": "autonomy",
        }

        monkeypatch.setattr(server.workspace_settings, "get_workspace", lambda _name: workspace)
        monkeypatch.setattr(server.workspace_settings, "validate_artifacts", lambda _proj: [])
        monkeypatch.setattr(server.workspace_settings, "artifact_mounts", lambda _proj: {})
        monkeypatch.setattr(server, "render_workspace_primer", lambda _proj: "primer")
        monkeypatch.setattr(server, "prepare_session_mounts", lambda *a, **kw: {})

        def fake_launch_session(**kwargs):
            launch_kwargs.update(kwargs)
            return "docker run cap"

        monkeypatch.setattr(server, "launch_session", fake_launch_session)

        resp = test_client.post(
            "/api/session/resume",
            json={"source_id": resume_env["container_source_id"]},
        )
        assert resp.status_code == 200
        assert "capabilities" in launch_kwargs, (
            "workspace resume must forward capabilities=... to launch_session"
        )
        assert launch_kwargs["capabilities"] == workspace.capabilities

    def test_default_terminal_create_path_does_not_forward_capabilities(
        self, test_client, monkeypatch,
    ):
        """Non-workspace create path must remain unchanged — no capabilities arg."""
        from tools.dashboard import server

        launch_kwargs: dict = {}

        def fake_launch_session(**kwargs):
            launch_kwargs.update(kwargs)
            return "docker run terminal"

        monkeypatch.setattr(server, "launch_session", fake_launch_session)
        monkeypatch.setattr(
            server.dashboard_db,
            "get_session",
            lambda tmux_name: {
                "tmux_name": tmux_name,
                "is_live": 1,
                "jsonl_path": "/tmp/fake/sessions",
                "type": "container",
                "label": "",
            },
        )

        resp = test_client.post("/api/session/create", json={})
        assert resp.status_code == 200
        # The default container-terminal path must not synthesize capabilities.
        assert launch_kwargs.get("capabilities", ()) == ()


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
