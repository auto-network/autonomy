"""Tests for catch_up_sweep() — the W4 manifest-driven sweep (auto-gah4g).

Covers the AC directly: a no-op steady-state pass opens zero GraphDB
connections; changed/new files get grouped by org and ingested with one
connection per org; sealing skips files that vanished; force= re-scans
and unseals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.ingest import catch_up_sweep


def _write_session(path: Path, org: str, text: str = "hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / ".session_meta.json").write_text(json.dumps({"graph_org": org}))
    entry = {
        "type": "user", "uuid": "u1",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-05-01T10:00:00Z",
    }
    path.write_text(json.dumps(entry) + "\n")


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


@pytest.fixture
def sweep_env(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)

    agent_runs = tmp_path / "data" / "agent-runs"
    agent_runs.mkdir(parents=True)
    monkeypatch.setattr("tools.graph.ingest._REPO_ROOT", tmp_path)
    # No host ~/.claude/projects in the sweep for these tests.
    monkeypatch.setattr("tools.graph.ingest.Path.home", lambda: tmp_path / "no-home")

    # Route dashboard.db (manifest) to a scratch file too.
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    import importlib
    from tools.dashboard.dao import dashboard_db as ddb
    importlib.reload(ddb)

    yield tmp_path, agent_runs, orgs_dir


class TestCatchUpSweepBasics:
    def test_first_pass_ingests_and_records_manifest(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        session_dir = agent_runs / "run-1" / "sessions"
        jsonl = session_dir / "uuid-1.jsonl"
        _write_session(jsonl, "autonomy")

        result = catch_up_sweep()

        assert result["scanned"] == 1
        assert result["changed"] == 1
        assert result["unchanged"] == 0
        assert result["db_opens"] == 1
        assert result["orgs_touched"] == 1
        assert result["results"][0]["status"] == "ingested"

        from tools.dashboard.dao import dashboard_db as ddb
        abs_path = str(jsonl.resolve())
        entry = ddb.get_manifest_entry(abs_path)
        assert entry is not None
        assert entry["org"] == "autonomy"
        assert entry["state"] == "active"

    def test_second_pass_no_growth_is_zero_db_opens(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        session_dir = agent_runs / "run-1" / "sessions"
        jsonl = session_dir / "uuid-1.jsonl"
        _write_session(jsonl, "autonomy")

        catch_up_sweep()
        result = catch_up_sweep()

        assert result["scanned"] == 1
        assert result["unchanged"] == 1
        assert result["changed"] == 0
        assert result["db_opens"] == 0, "steady-state pass must open zero GraphDB connections"

    def test_growth_reingests_only_changed_file(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        session_dir = agent_runs / "run-1" / "sessions"
        jsonl_a = session_dir / "uuid-a.jsonl"
        jsonl_b = session_dir / "uuid-b.jsonl"
        _write_session(jsonl_a, "autonomy", "session a")
        _write_session(jsonl_b, "autonomy", "session b")

        catch_up_sweep()

        with open(jsonl_a, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "assistant", "uuid": "a2",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "more"}], "model": "m"},
                "timestamp": "2026-05-01T10:01:00Z",
            }) + "\n")

        result = catch_up_sweep()
        assert result["changed"] == 1
        assert result["unchanged"] == 1
        assert result["db_opens"] == 1

    def test_groups_multiple_orgs_one_db_open_each(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        run1 = agent_runs / "run-1" / "sessions"
        run2 = agent_runs / "run-2" / "sessions"
        _write_session(run1 / "uuid-1.jsonl", "autonomy")
        _write_session(run2 / "uuid-2.jsonl", "personal")

        result = catch_up_sweep()

        assert result["changed"] == 2
        assert result["orgs_touched"] == 2
        assert result["db_opens"] == 2


class TestSealing:
    def test_vanished_file_gets_sealed(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        session_dir = agent_runs / "run-1" / "sessions"
        jsonl = session_dir / "uuid-1.jsonl"
        _write_session(jsonl, "autonomy")

        catch_up_sweep()
        jsonl.unlink()

        result = catch_up_sweep()
        assert result["sealed"] == 1

        from tools.dashboard.dao import dashboard_db as ddb
        entry = ddb.get_manifest_entry(str(jsonl.resolve()))
        assert entry["state"] == "sealed"

    def test_sealed_file_skipped_on_subsequent_pass(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        session_dir = agent_runs / "run-1" / "sessions"
        jsonl = session_dir / "uuid-1.jsonl"
        _write_session(jsonl, "autonomy")
        catch_up_sweep()
        jsonl.unlink()
        catch_up_sweep()  # seals it (file gone)

        # Recreate the file with different content — sweep should still
        # skip it (sealed rows are only cleared by --force).
        _write_session(jsonl, "autonomy", "resurrected")
        result = catch_up_sweep()
        assert result["changed"] == 0
        assert result["unchanged"] == 0  # not even compared — skipped outright

    def test_force_unseals_and_reingests(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        session_dir = agent_runs / "run-1" / "sessions"
        jsonl = session_dir / "uuid-1.jsonl"
        _write_session(jsonl, "autonomy")
        catch_up_sweep()
        jsonl.unlink()
        catch_up_sweep()  # sealed

        _write_session(jsonl, "autonomy", "resurrected")
        result = catch_up_sweep(force=True)
        assert result["changed"] == 1

        from tools.dashboard.dao import dashboard_db as ddb
        entry = ddb.get_manifest_entry(str(jsonl.resolve()))
        assert entry["state"] == "active"


class TestOrgResolutionSkip:
    def test_unresolvable_org_is_skipped_not_crashed(self, sweep_env):
        tmp_path, agent_runs, orgs_dir = sweep_env
        session_dir = agent_runs / "run-1" / "sessions"
        jsonl = session_dir / "uuid-1.jsonl"
        session_dir.mkdir(parents=True, exist_ok=True)
        # No .session_meta.json at all -> unresolvable org for this path shape.
        entry = {
            "type": "user", "uuid": "u1",
            "message": {"role": "user", "content": "hello"},
            "timestamp": "2026-05-01T10:00:00Z",
        }
        jsonl.write_text(json.dumps(entry) + "\n")

        result = catch_up_sweep()
        assert result["changed"] == 0
        assert result["db_opens"] == 0
