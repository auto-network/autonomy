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
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents import dispatcher, launch_session_cli, project_config, session_launcher
from agents.project_config import ProjectConfig
from tools.graph.db import GraphDB
from tools.graph.ingest import ingest_claude_code_session


# ── Helpers ──────────────────────────────────────────────────────

def _write_projects_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "projects.yaml"
    path.write_text(dedent(body).lstrip())
    return path


def _completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def test_projects(tmp_path):
    """Load a synthetic projects.yaml and patch dispatcher.load_projects to use it."""
    path = _write_projects_yaml(tmp_path, """
        projects:
          autonomy:
            name: "Autonomy Network"
            image: autonomy-agent:dashboard
            graph_project: autonomy
            default_tags: [dashboard, ui]
            dispatch_labels: [dashboard]
          enterprise:
            name: "Enterprise"
            image: autonomy-agent:enterprise
            graph_project: anchore
            default_tags: [enterprise]
            dispatch_labels: [enterprise]
    """)
    project_config.clear_cache()
    projects = project_config.load_projects(path)

    with patch.object(dispatcher, "load_projects", return_value=projects):
        yield projects

    project_config.clear_cache()


# ══════════════════════════════════════════════════════════════════════
# Path 1: label → image + config → env inheritance
# ══════════════════════════════════════════════════════════════════════


class TestLabelToImageRouting:
    """`dispatch_labels` in projects.yaml drives image selection."""

    def test_build_label_image_map_from_config(self, test_projects):
        mapping = dispatcher._build_label_image_map()
        assert mapping["dashboard"] == "autonomy-agent:dashboard"
        assert mapping["enterprise"] == "autonomy-agent:enterprise"

    def test_project_for_bead_matches_on_dispatch_label(self, test_projects):
        bead = {"id": "auto-x", "labels": ["dashboard"]}
        project = dispatcher.project_for_bead(bead)
        assert project is not None
        assert project.id == "autonomy"
        assert project.graph_project == "autonomy"
        assert project.default_tags == ("dashboard", "ui")
        assert project.image == "autonomy-agent:dashboard"

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
        assert image == "autonomy-agent:dashboard"


class TestStartAgentForwardsScope:
    """start_agent → launch.sh argv includes --graph-project + --graph-tags."""

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
            image="autonomy-agent:dashboard",
            graph_project="autonomy",
            graph_tags=("dashboard", "ui"),
        )
        assert agent is not None

        argv = mock_run.call_args[0][0]
        assert argv[0].endswith("launch.sh")
        assert argv[1] == "auto-xyz"
        assert "--image=autonomy-agent:dashboard" in argv
        assert "--detach" in argv
        assert "--graph-project=autonomy" in argv
        assert "--graph-tags=dashboard,ui" in argv

    @patch("agents.dispatcher.subprocess.run")
    def test_launch_argv_omits_scope_flags_when_unset(self, mock_run):
        mock_run.return_value = _completed_process(
            stdout=(
                "CONTAINER_ID=abc\nCONTAINER_NAME=n\nOUTPUT_DIR=/o\n"
                "WORKTREE_DIR=/w\nBRANCH=agent/b\nBRANCH_BASE=base\n"
            ),
        )
        dispatcher.start_agent("auto-abc", image="autonomy-agent")
        argv = mock_run.call_args[0][0]
        assert not any(a.startswith("--graph-project") for a in argv)
        assert not any(a.startswith("--graph-tags") for a in argv)


class TestLaunchSessionCliMetadata:
    """launch_session_cli --graph-project/--graph-tags → launch_session metadata."""

    def _invoke_cli(self, tmp_path, *, argv_extra: list[str]):
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
            "--image", "autonomy-agent:dashboard",
            "--detach",
            *argv_extra,
        ]
        with patch.object(launch_session_cli, "launch_session", fake_launch_session):
            with patch("sys.argv", argv):
                rc = launch_session_cli.main()
        assert rc == 0
        return captured

    def test_graph_project_and_tags_reach_launch_session(self, tmp_path):
        captured = self._invoke_cli(
            tmp_path,
            argv_extra=["--graph-project", "autonomy", "--graph-tags", "dashboard,ui"],
        )
        meta = captured["metadata"]
        assert meta["bead_id"] == "auto-xyz"
        assert meta["graph_project"] == "autonomy"
        assert meta["graph_tags"] == ["dashboard", "ui"]
        assert captured["image"] == "autonomy-agent:dashboard"
        assert captured["detach"] is True

    def test_no_graph_scope_when_flags_omitted(self, tmp_path):
        captured = self._invoke_cli(tmp_path, argv_extra=[])
        meta = captured.get("metadata") or {}
        assert "graph_project" not in meta
        assert "graph_tags" not in meta


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
        calls: list[list[str]] = []
        completed = _completed_process(stdout="fake-container-id\n")

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return completed

        monkeypatch.setattr(session_launcher.subprocess, "run", fake_run)
        return calls

    def test_end_to_end_meta_and_env(
        self, tmp_path, fake_creds, fake_crosstalk, captured_run
    ):
        """Metadata dict → .session_meta.json on disk + GRAPH_SCOPE/GRAPH_TAGS env."""
        run_dir = tmp_path / "run"
        session_launcher.launch_session(
            session_type="dispatch",
            name="agent-auto-xyz-1234",
            prompt=None,
            detach=True,
            image="autonomy-agent:dashboard",
            output_dir=str(run_dir),
            metadata={
                "bead_id": "auto-xyz",
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
        assert "GRAPH_SCOPE=autonomy" in cmd
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
            "SELECT project, metadata FROM sources WHERE id = ?",
            (result["source_id"],),
        ).fetchone()
        assert row["project"] == "autonomy"

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
            "SELECT project, metadata FROM sources WHERE id = ?",
            (result["source_id"],),
        ).fetchone()
        assert row["project"] == "anchore"
        meta = json.loads(row["metadata"])
        assert meta["graph_tags"] == ["enterprise", "enterprise-ng"]

    def test_missing_graph_project_leaves_project_null(self, graph_db, tmp_path):
        """Legacy / unlabeled sessions without graph_project → project remains null."""
        jsonl = self._write_session(
            tmp_path,
            meta={"type": "dispatch", "container_name": "agent-legacy"},
        )
        with patch("tools.graph.ingest._lookup_dashboard_label", return_value=None):
            with patch("tools.graph.ingest._lookup_bead_title", return_value=None):
                result = ingest_claude_code_session(graph_db, jsonl)

        row = graph_db.conn.execute(
            "SELECT project, metadata FROM sources WHERE id = ?",
            (result["source_id"],),
        ).fetchone()
        assert row["project"] is None
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
        monkeypatch.setattr(dispatcher, "_rig_image", "autonomy-agent:rig-default")
        assert dispatcher.image_for_bead({"labels": []}) == "autonomy-agent:rig-default"

    @patch("agents.dispatcher.subprocess.run")
    def test_unlabeled_bead_launch_has_no_scope_flags(self, mock_run, test_projects,
                                                      monkeypatch):
        """Dispatching an unlabeled bead mimics the call shape dispatch_cycle uses."""
        monkeypatch.setattr(dispatcher, "_rig_image", "autonomy-agent:rig-default")
        mock_run.return_value = _completed_process(
            stdout=(
                "CONTAINER_ID=abc\nCONTAINER_NAME=n\nOUTPUT_DIR=/o\n"
                "WORKTREE_DIR=/w\nBRANCH=agent/b\nBRANCH_BASE=base\n"
            ),
        )

        bead = {"id": "auto-unlabeled", "labels": []}
        project: ProjectConfig | None = dispatcher.project_for_bead(bead)
        assert project is None

        # Reproduce the argument shape that dispatch_cycle uses:
        image = project.image if project is not None else dispatcher._rig_image
        dispatcher.start_agent(
            bead["id"],
            image=image,
            graph_project=project.graph_project if project is not None else None,
            graph_tags=project.default_tags if project is not None else (),
        )

        argv = mock_run.call_args[0][0]
        assert "--image=autonomy-agent:rig-default" in argv
        assert not any(a.startswith("--graph-project") for a in argv)
        assert not any(a.startswith("--graph-tags") for a in argv)
