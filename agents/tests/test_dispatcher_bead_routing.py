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
    """A DATA_ROOT with one provisioned org tracker (prefix anc)."""
    orgs = tmp_path / ".beads" / "orgs" / "anchore"
    orgs.mkdir(parents=True)
    (orgs / "metadata.json").write_text(json.dumps(
        {"dolt_database": "anchore", "prefix": "anc-"}))
    monkeypatch.setattr(disp, "DATA_ROOT", tmp_path)
    disp._bead_prefix_cache.update({"at": 0.0, "map": {}})
    yield SimpleNamespace(root=tmp_path, anchore=orgs)
    disp._bead_prefix_cache.update({"at": 0.0, "map": {}})


def test_prefix_map_and_inference(org_tree):
    assert disp._bead_prefix_map() == {"anc": org_tree.anchore}
    assert disp._beads_dir_for_args(
        ["update", "anc-123", "--append-notes", "x"]) == org_tree.anchore
    assert disp._beads_dir_for_args(["update", "auto-99x", "-s", "open"]) is None
    assert disp._beads_dir_for_args(["query", "status=open", "--json"]) is None


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
