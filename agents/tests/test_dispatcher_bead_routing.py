"""Dispatcher bd credential + per-org tracker routing (2026-08-28 handoff).

Defect A: bd inherited a bare environment, fell back to user 'root', and
was denied on every query — bead dispatch silently dead. Defect B: only
the shared tracker was ever queried, so provisioned orgs' beads were
invisible. Every invocation now carries its tracker's credentials, and
id-bearing calls route by bead-id prefix.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents import dispatcher as disp


@pytest.fixture
def org_tree(tmp_path, monkeypatch):
    """A DATA_ROOT with one provisioned org tracker (prefix anc).

    metadata.json deliberately carries NO prefix key — matching the real
    provisioning recipe (graph 74e2b864), which writes issue_prefix only
    into the database's config table. The prefix map is pre-seeded for
    tests that exercise routing rather than discovery.
    """
    import time as _time
    orgs = tmp_path / ".beads" / "orgs" / "anchore"
    orgs.mkdir(parents=True)
    (orgs / "metadata.json").write_text(json.dumps(
        {"dolt_database": "anchore", "backend": "dolt"}))
    monkeypatch.setattr(disp, "DATA_ROOT", tmp_path)
    disp._bead_prefix_cache.update(
        {"at": _time.time() + 3600, "map": {"anc": orgs}})
    yield SimpleNamespace(root=tmp_path, anchore=orgs)
    disp._bead_prefix_cache.update({"at": 0.0, "map": {}})


def test_prefix_discovery_reads_the_config_table(org_tree, monkeypatch):
    """The authoritative issue_prefix lives in the tracker DATABASE's
    config table (bd config get), never metadata.json — anchore's real
    metadata has no prefix key, and routing must still work."""
    disp._bead_prefix_cache.update({"at": 0.0, "map": {}})
    config_calls = []

    def fake_run_bd(args, timeout=15, check=False, beads_dir=None):
        assert args == ["config", "get", "issue_prefix"]
        config_calls.append(beads_dir)
        return "anc\n"

    monkeypatch.setattr(disp, "run_bd", fake_run_bd)
    assert disp._bead_prefix_map() == {"anc": org_tree.anchore}
    assert config_calls == [org_tree.anchore]


def test_prefix_falls_back_to_metadata_when_config_empty(org_tree, monkeypatch):
    disp._bead_prefix_cache.update({"at": 0.0, "map": {}})
    (org_tree.anchore / "metadata.json").write_text(json.dumps(
        {"dolt_database": "anchore", "prefix": "anc-"}))
    monkeypatch.setattr(
        disp, "run_bd",
        lambda args, timeout=15, check=False, beads_dir=None: "")
    assert disp._bead_prefix_map() == {"anc": org_tree.anchore}


def test_prefix_map_and_inference(org_tree):
    assert disp._beads_dir_for_args(
        ["update", "anc-123", "--append-notes", "x"]) == org_tree.anchore
    assert disp._beads_dir_for_args(["update", "auto-99x", "-s", "open"]) is None
    assert disp._beads_dir_for_args(["query", "status=open", "--json"]) is None
    assert disp._beads_dir_for_args(["set-state", "anc-9k", "readiness=blocked"]) \
        == org_tree.anchore


def test_run_bd_carries_tracker_credentials(org_tree, monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    def fake_client_env(beads_dir=None):
        if beads_dir is None:
            return {"BEADS_DOLT_SERVER_USER": "beads_autonomy"}
        return {"BEADS_DOLT_SERVER_USER": "beads_anchore",
                "BEADS_DIR": str(beads_dir)}

    monkeypatch.setattr(disp.subprocess, "run", fake_run)
    import tools.data_paths as data_paths
    monkeypatch.setattr(data_paths, "beads_client_env", fake_client_env)

    disp.run_bd(["show", "anc-42", "--json"])
    assert captured["env"]["BEADS_DOLT_SERVER_USER"] == "beads_anchore"
    assert captured["env"]["BEADS_DIR"] == str(org_tree.anchore)

    disp.run_bd(["show", "auto-42", "--json"])
    assert captured["env"]["BEADS_DOLT_SERVER_USER"] == "beads_autonomy"
    # ambient env may carry its own BEADS_DIR (session containers do);
    # what matters is the shared call did NOT route to the org dir
    assert captured["env"].get("BEADS_DIR") != str(org_tree.anchore)

    disp.run_bd(["query", "status=open", "--json"], beads_dir=org_tree.anchore)
    assert captured["env"]["BEADS_DOLT_SERVER_USER"] == "beads_anchore"


def test_get_ready_beads_merges_all_trackers(org_tree, monkeypatch):
    calls = []

    def fake_run_bd(args, timeout=15, check=False, beads_dir=None):
        calls.append(beads_dir)
        if beads_dir is None:
            return json.dumps([{"id": "auto-1", "title": "shared"}])
        return json.dumps([{"id": "anc-1", "title": "org"}])

    monkeypatch.setattr(disp, "run_bd", fake_run_bd)
    beads = disp.get_ready_beads()
    assert {b["id"] for b in beads} == {"auto-1", "anc-1"}
    assert calls == [None, org_tree.anchore]


def test_same_bead_via_two_trackers_dedupes_to_one(org_tree, monkeypatch):
    """The shared dir and the autonomy org dir name the SAME database
    during the transition — a bead visible through both must yield one
    candidate, never a double launch (host caveat, 2026-08-29: wired but
    unobserved live while zero beads were approved)."""
    def fake_run_bd(args, timeout=15, check=False, beads_dir=None):
        # identical row from the shared tracker and the org dir
        return json.dumps([{"id": "auto-dup1", "title": "same bead"}])

    monkeypatch.setattr(disp, "run_bd", fake_run_bd)
    beads = disp.get_ready_beads()
    assert [b["id"] for b in beads] == ["auto-dup1"]


def test_run_bd_defaults_beads_dir_to_the_data_root_tracker(monkeypatch):
    """The Compose dispatcher has no BEADS_DIR in its environment; without a
    default bd searched its cwd, logged "no beads database found" every cycle
    and fell back to the API path. Mirror the dashboard's run_cli default."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw.get("env")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    import tools.data_paths as data_paths
    monkeypatch.setattr(disp.subprocess, "run", fake_run)
    monkeypatch.setattr(data_paths, "beads_client_env", lambda beads_dir=None: {})
    monkeypatch.delenv("BEADS_DIR", raising=False)

    disp.run_bd(["query", 'status=open AND label="readiness:approved"', "--json"], beads_dir=None)
    assert captured["env"]["BEADS_DIR"] == str(disp.DATA_ROOT / ".beads")

    # An ambient BEADS_DIR still wins (session containers set their own).
    monkeypatch.setenv("BEADS_DIR", "/elsewhere/.beads")
    disp.run_bd(["query", "status=open", "--json"], beads_dir=None)
    assert captured["env"]["BEADS_DIR"] == "/elsewhere/.beads"
