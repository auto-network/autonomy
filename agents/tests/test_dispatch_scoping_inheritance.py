"""End-to-end acceptance test for dispatched-bead graph scoping inheritance.

Validates auto-elmo (commit 8f85239): a bead's dispatch label flows through
``projects.yaml`` → dispatcher routing → ``launch.sh`` argv →
``launch_session_cli`` metadata → ``launch_session`` env + ``.session_meta.json``
→ ``tools.graph.ingest`` project / tag overlay.

Three paths are covered:

1. Label → image + config → env inheritance.
2. Ingested session carries the scope from ``.session_meta.json``.
3. Unlabeled bead falls back to the rig default image with no GRAPH_SCOPE
   / GRAPH_TAGS override.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents import dispatcher, launch_session_cli, session_launcher
from agents.workspace_settings import WorkspaceV1 as ProjectConfig
from tools.graph.db import GraphDB
from tools.graph.ingest import ingest_claude_code_session


# ── Helpers ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _stub_platform_snapshot(monkeypatch, tmp_path):
    """Never run the real snapshot git (clone/fetch of the live checkout)."""
    monkeypatch.setattr(
        session_launcher,
        "_ensure_platform_snapshot",
        lambda: str(tmp_path / "platform-snapshot"),
    )


def _completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def test_projects():
    """Patch ``dispatcher.load_workspaces`` with a synthetic Settings-backed registry."""
    projects = {
        "autonomy": ProjectConfig(
            id="autonomy",
            name="Autonomy Network",
            description="",
            image="autonomy-session-platform",
            graph_project="autonomy",
            harness="claude",
            default_tags=("dashboard", "ui"),
            dispatch_labels=("dashboard",),
        ),
        "enterprise": ProjectConfig(
            id="enterprise",
            name="Enterprise",
            description="",
            image="session-enterprise",
            graph_project="anchore",
            harness="codex",
            default_tags=("enterprise",),
            dispatch_labels=("enterprise",),
        ),
    }
    with patch.object(dispatcher, "load_workspaces", return_value=projects):
        yield projects


# ══════════════════════════════════════════════════════════════════════
# Path 1: label → image + config → env inheritance
# ══════════════════════════════════════════════════════════════════════


class TestLabelToImageRouting:
    """`dispatch_labels` in projects.yaml drives image selection."""

    def test_build_label_image_map_from_config(self, test_projects):
        mapping = dispatcher._build_label_image_map()
        assert mapping["dashboard"] == "autonomy-session-platform"
        assert mapping["enterprise"] == "session-enterprise"

    def test_project_for_bead_matches_on_dispatch_label(self, test_projects):
        bead = {"id": "auto-x", "labels": ["dashboard"]}
        project = dispatcher.project_for_bead(bead)
        assert project is not None
        assert project.id == "autonomy"
        assert project.graph_project == "autonomy"
        assert project.default_tags == ("dashboard", "ui")
        assert project.image == "autonomy-session-platform"

    def test_project_for_bead_first_label_wins(self, test_projects):
        """Bead with multiple matching labels: first project in registry wins."""
        bead = {"id": "auto-x", "labels": ["enterprise"]}
        project = dispatcher.project_for_bead(bead)
        assert project is not None
        assert project.id == "enterprise"
        assert project.graph_project == "anchore"
        assert project.default_tags == ("enterprise",)

    def test_image_for_bead_uses_project_image(self, test_projects):
        image = dispatcher.image_for_bead({"labels": ["dashboard"]})
        assert image == "autonomy-session-platform"


class TestStartAgentForwardsScope:
    """start_agent → launch.sh argv includes harness + scope flags."""

    @patch("agents.dispatcher.subprocess.run")
    def test_launch_argv_carries_scope_flags(self, mock_run):
        mock_run.return_value = _completed_process(
            stdout=(
                "CONTAINER_ID=abc123\n"
                "CONTAINER_NAME=agent-auto-xyz-1234\n"
                "OUTPUT_DIR=/out\n"
                "WORKTREE_DIR=/wt\n"
                "BRANCH=agent/auto-xyz\n"
                "BRANCH_BASE=base\n"
            ),
        )

        agent = dispatcher.start_agent(
            "auto-xyz",
            image="autonomy-session-platform",
            harness="codex",
            graph_project="autonomy",
            graph_tags=("dashboard", "ui"),
            workspace_id="autonomy-codex",
        )
        assert agent is not None

        argv = mock_run.call_args[0][0]
        assert argv[0].endswith("launch.sh")
        assert argv[1] == "auto-xyz"
        assert "--image=autonomy-session-platform" in argv
        assert "--harness=codex" in argv
        assert "--detach" in argv
        assert "--graph-project=autonomy" in argv
        assert "--graph-tags=dashboard,ui" in argv
        assert "--workspace-id=autonomy-codex" in argv

    @patch("agents.dispatcher.subprocess.run")
    def test_launch_argv_omits_scope_flags_when_unset(self, mock_run):
        mock_run.return_value = _completed_process(
            stdout=(
                "CONTAINER_ID=abc\nCONTAINER_NAME=n\nOUTPUT_DIR=/o\n"
                "WORKTREE_DIR=/w\nBRANCH=agent/b\nBRANCH_BASE=base\n"
            ),
        )
        dispatcher.start_agent("auto-abc", image="autonomy-session")
        argv = mock_run.call_args[0][0]
        assert "--harness=claude" in argv
        assert not any(a.startswith("--graph-project") for a in argv)
        assert not any(a.startswith("--graph-tags") for a in argv)


class TestStartLibrarianHarness:
    """Librarians derive harness from the autonomy workspace when available."""

    def test_librarian_uses_autonomy_workspace_harness(self, test_projects):
        job = {"id": "lib-1234", "job_type": "review_report", "payload": "{}"}
        captured: dict = {}

        def fake_launch_session(**kwargs):
            captured.update(kwargs)
            return "fake-container-id"

        with patch.object(dispatcher, "_build_librarian_prompt", return_value="prompt"):
            with patch.object(dispatcher, "launch_session", fake_launch_session):
                lib = dispatcher.start_librarian(job)

        assert lib is not None
        assert captured["harness"] == "claude"
        assert captured["model"] == dispatcher.DEFAULT_SONNET_MODEL

    def test_librarian_omits_claude_model_for_codex_harness(self, test_projects):
        codex_projects = dict(test_projects)
        codex_projects["autonomy"] = ProjectConfig(
            id="autonomy",
            name="Autonomy Network",
            description="",
            image="autonomy-session-platform",
            graph_project="autonomy",
            harness="codex",
            default_tags=("dashboard", "ui"),
            dispatch_labels=("dashboard",),
        )
        job = {"id": "lib-5678", "job_type": "review_report", "payload": "{}"}
        captured: dict = {}

        def fake_launch_session(**kwargs):
            captured.update(kwargs)
            return "fake-container-id"

        with patch.object(dispatcher, "load_workspaces", return_value=codex_projects):
            with patch.object(dispatcher, "_build_librarian_prompt", return_value="prompt"):
                with patch.object(dispatcher, "launch_session", fake_launch_session):
                    lib = dispatcher.start_librarian(job)

        assert lib is not None
        assert captured["harness"] == "codex"
        assert captured["model"] is None


class TestLaunchSessionCliMetadata:
    """launch_session_cli --org/--graph-tags → launch_session metadata."""

    def _invoke_cli(self, tmp_path, *, argv_extra: list[str], harness: str = "claude"):
        """Run launch_session_cli.main() in --detach mode with launch_session stubbed."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("prompt body")
        output_dir = tmp_path / "run"
        output_dir.mkdir()

        captured: dict = {}

        def fake_launch_session(**kwargs):
            captured.update(kwargs)
            return "fake-container-id"

        argv = [
            "launch_session_cli",
            "--session-type", "dispatch",
            "--name", "agent-auto-xyz-1234",
            "--prompt-file", str(prompt_file),
            "--bead-id", "auto-xyz",
            "--output-dir", str(output_dir),
            "--image", "autonomy-session-platform",
            "--harness", harness,
            "--detach",
            *argv_extra,
        ]
        with patch.object(launch_session_cli, "launch_session", fake_launch_session):
            with patch("sys.argv", argv):
                rc = launch_session_cli.main()
        assert rc == 0
        return captured

    def test_org_and_tags_reach_launch_session(self, tmp_path):
        captured = self._invoke_cli(
            tmp_path,
            argv_extra=["--org", "autonomy", "--graph-tags", "dashboard,ui"],
        )
        meta = captured["metadata"]
        assert meta["bead_id"] == "auto-xyz"
        assert meta["org"] == "autonomy"
        assert meta["graph_tags"] == ["dashboard", "ui"]
        assert captured["image"] == "autonomy-session-platform"
        assert captured["detach"] is True

    def test_graph_project_alias_reaches_launch_session_as_org(self, tmp_path):
        """--graph-project is a deprecated, warned alias for --org."""
        captured = self._invoke_cli(
            tmp_path, argv_extra=["--graph-project", "autonomy"],
        )
        meta = captured["metadata"]
        assert meta["org"] == "autonomy"
        assert "graph_project" not in meta

    def test_missing_org_fails_with_no_silent_default(self, tmp_path):
        """Org is required — omitting both --org and --graph-project fails
        loudly instead of silently routing to personal.db."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("prompt body")
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        argv = [
            "launch_session_cli",
            "--session-type", "dispatch",
            "--name", "agent-auto-xyz-1234",
            "--prompt-file", str(prompt_file),
            "--bead-id", "auto-xyz",
            "--output-dir", str(output_dir),
            "--image", "autonomy-session-platform",
            "--harness", "claude",
            "--detach",
        ]
        with patch("sys.argv", argv):
            rc = launch_session_cli.main()
        assert rc == 1

    def test_no_graph_tags_when_flag_omitted(self, tmp_path):
        captured = self._invoke_cli(tmp_path, argv_extra=["--org", "autonomy"])
        meta = captured.get("metadata") or {}
        assert "graph_tags" not in meta

    def test_harness_reaches_launch_session(self, tmp_path):
        captured = self._invoke_cli(tmp_path, argv_extra=["--org", "autonomy"], harness="codex")
        assert captured["harness"] == "codex"
        assert captured["model"] is None

    def test_workspace_runtime_reaches_launch_session(self, tmp_path):
        workspace = SimpleNamespace(
            graph_project="autonomy",
            model=None,
            needs_nested_docker=True,
            session_runtime="sysbox",
        )
        with patch.object(
            launch_session_cli,
            "load_workspaces",
            return_value={"anchore": workspace},
        ):
            captured = self._invoke_cli(
                tmp_path,
                argv_extra=["--workspace-id", "anchore"],
            )
        assert captured["needs_nested_docker"] is True
        assert captured["runtime"] == "sysbox"


class TestLaunchSessionMetaAndEnv:
    """launch_session writes .session_meta.json and exports GRAPH_SCOPE/TAGS."""

    @pytest.fixture
    def fake_creds(self, monkeypatch):
        monkeypatch.setattr(
            session_launcher,
            "_resolve_credentials",
            lambda: {"type": "token", "token": "tok"},
        )

    @pytest.fixture
    def fake_crosstalk(self, monkeypatch):
        fake = SimpleNamespace(insert_token=lambda *a, **kw: None)
        fake_dao = SimpleNamespace(auth_db=fake)
        monkeypatch.setitem(__import__("sys").modules, "tools.dashboard.dao", fake_dao)

    @pytest.fixture
    def captured_run(self, monkeypatch):
        # Docker commands only — the launch path also shells out to helpers
        # (git rev-parse for Codex trust rooting) that would shift indices.
        calls: list[list[str]] = []
        completed = _completed_process(stdout="fake-container-id\n")

        def fake_run(cmd, **kwargs):
            # `docker image inspect` is a read-only metadata call the launcher
            # makes to bake harness versions into meta — not a launch command,
            # and capturing it would shift captured_run[0] off the docker run.
            if (cmd and cmd[0] == "docker"
                    and not (len(cmd) > 2 and cmd[1] == "image"
                             and cmd[2] == "inspect")):
                calls.append(cmd)
            return completed

        monkeypatch.setattr(session_launcher.subprocess, "run", fake_run)
        return calls

    def test_end_to_end_meta_and_env(
        self, tmp_path, fake_creds, fake_crosstalk, captured_run
    ):
        """Metadata dict → .session_meta.json on disk + GRAPH_TAGS env."""
        run_dir = tmp_path / "run"
        session_launcher.launch_session(
            session_type="dispatch",
            name="agent-auto-xyz-1234",
            prompt=None,
            detach=True,
            image="autonomy-session-platform",
            output_dir=str(run_dir),
            metadata={
                "bead_id": "auto-xyz",
                "org": "autonomy",
                "graph_project": "autonomy",
                "graph_tags": ["dashboard", "ui"],
            },
        )

        meta_file = run_dir / "sessions" / ".session_meta.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text())
        assert meta["bead_id"] == "auto-xyz"
        assert meta["graph_project"] == "autonomy"
        assert meta["graph_tags"] == ["dashboard", "ui"]

        cmd = captured_run[0]
        assert "GRAPH_TAGS=dashboard,ui" in cmd


# ══════════════════════════════════════════════════════════════════════
# Path 2: ingested session carries the scope
# ══════════════════════════════════════════════════════════════════════


class TestIngestHonorsSessionMeta:
    """ingest_claude_code_session reads graph_project/graph_tags from meta."""

    def _user_entry(self, text: str, ts: str = "2026-04-19T10:00:00Z") -> dict:
        return {
            "type": "user",
            "uuid": f"u-{abs(hash(text)) & 0xffff:x}",
            "message": {"role": "user", "content": text},
            "timestamp": ts,
        }

    def _assistant_entry(self, text: str, ts: str = "2026-04-19T10:00:05Z") -> dict:
        return {
            "type": "assistant",
            "uuid": f"a-{abs(hash(text)) & 0xffff:x}",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": "claude-test",
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
            "timestamp": ts,
        }

    def _write_session(self, tmp_path: Path, meta: dict) -> Path:
        """Build a fake dispatched-session directory layout and return the JSONL path.

        Mirrors the production layout: ``run_dir/sessions/.session_meta.json`` plus
        ``run_dir/sessions/<project>/<uuid>.jsonl``.
        """
        run_dir = tmp_path / "agent-run"
        sessions_dir = run_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / ".session_meta.json").write_text(json.dumps(meta))

        project_dir = sessions_dir / "-workspace-repo"
        project_dir.mkdir()
        jsonl = project_dir / "fa5a5a5a-test-uuid-0001.jsonl"
        with jsonl.open("w") as f:
            f.write(json.dumps(self._user_entry("Hello there", ts="2026-04-19T10:00:00Z")) + "\n")
            f.write(json.dumps(self._assistant_entry("General Kenobi", ts="2026-04-19T10:00:05Z")) + "\n")
        return jsonl

    @pytest.fixture
    def graph_db(self, tmp_path):
        db = GraphDB(tmp_path / "graph.db")
        yield db
        db.close()

    def test_source_scoped_by_graph_project_from_meta(self, graph_db, tmp_path):
        jsonl = self._write_session(
            tmp_path,
            meta={
                "type": "dispatch",
                "container_name": "agent-auto-xyz",
                "bead_id": "auto-xyz",
                "graph_project": "autonomy",
                "graph_tags": ["dashboard", "ui"],
            },
        )

        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            with patch("tools.graph.ingest._lookup_bead_title", return_value=None):
                result = ingest_claude_code_session(graph_db, jsonl)
        assert result["status"] == "ingested"

        row = graph_db.conn.execute(
            "SELECT metadata FROM sources WHERE id = ?",
            (result["source_id"],),
        ).fetchone()

        meta = json.loads(row["metadata"])
        assert meta["graph_project"] == "autonomy"
        assert meta["graph_tags"] == ["dashboard", "ui"]
        assert meta["bead_id"] == "auto-xyz"
        assert meta["session_type"] == "dispatch"

    def test_anchore_scope_inherited(self, graph_db, tmp_path):
        """Confirm the same mechanism works for a different org (enterprise)."""
        jsonl = self._write_session(
            tmp_path,
            meta={
                "type": "dispatch",
                "bead_id": "auto-ent",
                "graph_project": "anchore",
                "graph_tags": ["enterprise", "enterprise-ng"],
            },
        )
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            with patch("tools.graph.ingest._lookup_bead_title", return_value=None):
                result = ingest_claude_code_session(graph_db, jsonl)

        row = graph_db.conn.execute(
            "SELECT metadata FROM sources WHERE id = ?",
            (result["source_id"],),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["graph_tags"] == ["enterprise", "enterprise-ng"]

    def test_missing_graph_project_leaves_meta_unset(self, graph_db, tmp_path):
        """Legacy / unlabeled sessions without graph_project → no graph_project/graph_tags in meta."""
        jsonl = self._write_session(
            tmp_path,
            meta={"type": "dispatch", "container_name": "agent-legacy"},
        )
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            with patch("tools.graph.ingest._lookup_bead_title", return_value=None):
                result = ingest_claude_code_session(graph_db, jsonl)

        row = graph_db.conn.execute(
            "SELECT metadata FROM sources WHERE id = ?",
            (result["source_id"],),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert "graph_project" not in meta
        assert "graph_tags" not in meta


# ══════════════════════════════════════════════════════════════════════
# Path 3: unlabeled bead falls back cleanly
# ══════════════════════════════════════════════════════════════════════


class TestUnlabeledBeadFallback:
    """Beads without a project-matching label use the rig default with no scope."""

    def test_project_for_bead_returns_none_when_no_labels(self, test_projects):
        assert dispatcher.project_for_bead({"id": "auto-x", "labels": []}) is None
        assert dispatcher.project_for_bead({"id": "auto-y"}) is None

    def test_project_for_bead_returns_none_when_label_unknown(self, test_projects):
        bead = {"id": "auto-x", "labels": ["does-not-exist"]}
        assert dispatcher.project_for_bead(bead) is None

    def test_image_for_bead_falls_back_to_rig_image(self, test_projects, monkeypatch):
        """With no matching project, image comes from the rig default."""
        monkeypatch.setattr(dispatcher, "_rig_image", "autonomy-session-rig-default")
        assert dispatcher.image_for_bead({"labels": []}) == "autonomy-session-rig-default"

    @patch("agents.dispatcher.subprocess.run")
    def test_unlabeled_bead_launch_defaults_graph_project_to_autonomy(
        self, mock_run, test_projects, monkeypatch,
    ):
        """Unlabeled beads still ship ``--graph-project=autonomy`` so the
        session's ``.session_meta.json`` carries a routing slug. Without
        this default, ingest cannot resolve a target org and (under the
        fail-closed policy) skips the session entirely — leaving live
        rig dispatches invisible to consumers."""
        monkeypatch.setattr(dispatcher, "_rig_image", "autonomy-session-rig-default")
        mock_run.return_value = _completed_process(
            stdout=(
                "CONTAINER_ID=abc\nCONTAINER_NAME=n\nOUTPUT_DIR=/o\n"
                "WORKTREE_DIR=/w\nBRANCH=agent/b\nBRANCH_BASE=base\n"
            ),
        )

        bead = {"id": "auto-unlabeled", "labels": []}
        project: ProjectConfig | None = dispatcher.project_for_bead(bead)
        assert project is None

        # Reproduce the argument shape that dispatch_cycle uses post-fix.
        image = project.image if project is not None else dispatcher._rig_image
        graph_project = (
            project.graph_project if project is not None else "autonomy"
        )
        dispatcher.start_agent(
            bead["id"],
            image=image,
            graph_project=graph_project,
            graph_tags=project.default_tags if project is not None else (),
        )

        argv = mock_run.call_args[0][0]
        assert "--image=autonomy-session-rig-default" in argv
        assert "--graph-project=autonomy" in argv
        # No tags for an unlabeled bead — only the routing slug.
        assert not any(a.startswith("--graph-tags") for a in argv)
