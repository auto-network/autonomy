"""The mission CLI (entrypoints/cli.py): substrate mounting, and verb
behavior against a recorded fake dashboard API.
"""
from __future__ import annotations

import argparse
import json

import pytest

from tools.dashboard.plugins.mission.entrypoints import cli

MID = "eec0efa1-fba7-42bf-8fd9-4790166b8f59"


class FakeApi:
    def __init__(self, canned):
        self.canned = canned
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        for (m, prefix), resp in self.canned.items():
            if m == method and path.startswith(prefix):
                return resp
        return {}


@pytest.fixture()
def parser():
    p = argparse.ArgumentParser(prog="graph")
    cli.register(p.add_subparsers(dest="cmd"))
    return p


def _run(parser, argv, monkeypatch, canned):
    fake = FakeApi(canned)
    monkeypatch.setattr(cli, "_api", lambda: fake)
    args = parser.parse_args(argv)
    args.func(args)
    return fake


BASE = {
    ("GET", "/api/mission/missions"): {"missions": [
        {"mission_id": MID, "name": "Multi-User Autonomy",
         "status": "active"}]},
    ("GET", f"/api/mission/pillars/{MID}"): {"pillars": [
        {"pillar_id": "relay", "name": "Relay"}]},
    ("GET", f"/api/mission/items/{MID}"): {"items": [
        {"surface_id": "relay", "item_id": "crit-a", "kind": "checkpoint",
         "state": "confirmed", "title": "Joins", "refs": ["bead:auto-1"]},
        {"surface_id": "relay", "item_id": "crit-b", "kind": "checkpoint",
         "state": "in_progress", "title": "Syncs"},
        {"surface_id": "relay", "item_id": "q-run", "kind": "question",
         "state": "open", "blocking": True, "title": "Who provisions?"}]},
    ("GET", f"/api/mission/tasks/{MID}"): {"tasks": {"relay": [
        {"id": "auto-1", "state": "complete", "title": "Close codes"},
        {"id": "auto-2", "state": "running", "title": "Tunnels"}]}},
}


def test_status_composes_ladders_and_blockers(parser, monkeypatch, capsys):
    _run(parser, ["mission", "status", "Multi-User"], monkeypatch, BASE)
    out = capsys.readouterr().out
    assert "delivery 1/2 (1 in progress)" in out
    assert "tasks 1/2 (1 running)" in out
    assert "1 open question" in out
    assert "BLOCKED on: Who provisions?" in out


def test_mission_resolution_by_name_substring(parser, monkeypatch, capsys):
    fake = _run(parser, ["mission", "items", "multi-user", "--kind",
                         "question"], monkeypatch, BASE)
    out = capsys.readouterr().out
    assert "q-run" in out and "crit-a" not in out
    assert any(p.startswith(f"/api/mission/items/{MID}")
               for _, p, _ in fake.calls)


def test_add_builds_v2_payload(parser, monkeypatch):
    fake = _run(parser, ["mission", "add", MID, "relay", "q-new",
                         "--kind", "question", "--state", "open",
                         "--title", "New?", "--blocking",
                         "--asked-by", "Jeremy"], monkeypatch, BASE)
    method, path, body = fake.calls[-1]
    assert (method, path) == (
        "PUT", f"/api/mission/item/{MID}/relay/q-new")
    assert body == {"kind": "question", "state": "open", "title": "New?",
                    "blocking": True, "asked_by": "Jeremy"}


def test_entry_verbs_hit_their_routes(parser, monkeypatch):
    for verb, route in (("work", "work"), ("reply", "reply"),
                        ("progress", "progress"), ("answer", "answer")):
        fake = _run(parser, ["mission", verb, MID, "relay", "it", "hello"],
                    monkeypatch, BASE)
        method, path, body = fake.calls[-1]
        assert method == "POST" and path.endswith(f"/it/{route}")
        assert body == {"text": "hello"}


def test_state_carries_turn(parser, monkeypatch):
    fake = _run(parser, ["mission", "state", MID, "relay", "crit-b",
                         "confirmed", "--turn", "17"], monkeypatch, BASE)
    method, path, body = fake.calls[-1]
    assert path.endswith("/crit-b/state")
    assert body == {"state": "confirmed", "turn": 17}


def test_chat_send_and_read(parser, monkeypatch, capsys):
    canned = dict(BASE)
    canned[("GET", f"/api/mission/chat/{MID}/relay")] = {"entries": [
        {"by": "Jeremy", "at": "2026-08-24T00:00:00Z", "text": "hi"}]}
    fake = _run(parser, ["mission", "chat", MID, "relay", "yo"],
                monkeypatch, canned)
    assert fake.calls[-1][0] == "POST"
    _run(parser, ["mission", "chat", MID, "relay"], monkeypatch, canned)
    assert "Jeremy: hi" in capsys.readouterr().out


def test_coverage_reports_both_gaps(parser, monkeypatch, capsys):
    canned = dict(BASE)
    # THREE tasks, TWO uncovered in the SAME pillar: pinned because
    # sorted(uncovered) over (pid, dict) tuples crashed on the pid tie
    # (dicts aren't orderable) — found live by the OSS Insights V2
    # migration coordinator on the first real non-empty uncovered list.
    canned[("GET", f"/api/mission/tasks/{MID}")] = {"tasks": {"relay": [
        {"id": "auto-1", "state": "complete", "title": "Close codes"},
        {"id": "auto-3", "state": "defined", "title": "Also uncovered"},
        {"id": "auto-2", "state": "running", "title": "Tunnels"}]}}
    canned[("GET", f"/api/mission/items/{MID}")] = {"items": [
        {"surface_id": "relay", "item_id": "c", "kind": "checkpoint",
         "state": "pending", "title": "t",
         "refs": ["bead:auto-1", "bead:auto-ghost"]}]}
    _run(parser, ["mission", "coverage", MID], monkeypatch, canned)
    out = capsys.readouterr().out
    assert "3 tasks, 1 criteria covering 2, 2 uncovered" in out
    assert out.index("auto-2") < out.index("auto-3")   # sorted by id
    assert "auto-ghost" in out


def test_substrate_mounts_only_when_enabled(monkeypatch):
    from tools.graph.plugin_cli import register_plugin_commands
    sub = argparse.ArgumentParser(prog="graph").add_subparsers(dest="cmd")
    assert register_plugin_commands(
        sub, payload_reader=lambda org: {}) == []
    sub2 = argparse.ArgumentParser(prog="graph").add_subparsers(dest="cmd")
    mounted = register_plugin_commands(
        sub2, payload_reader=lambda org: {"mission": {"enabled": True}})
    assert mounted == ["mission"]
    assert "mission" in sub2.choices


def test_update_refs_append_never_wipe(parser, monkeypatch):
    """Three real incidents: --ref on update silently replaced the list
    and coverage broke. Update APPENDS (deduped); --clear-refs is the
    only shrink path; untouched updates keep the list whole."""
    canned = dict(BASE)
    canned[("GET", f"/api/mission/items/{MID}")] = {"items": [
        {"surface_id": "relay", "item_id": "c1", "kind": "checkpoint",
         "state": "pending", "title": "t", "key": "relay:c1",
         "created_at": "c", "updated_at": "u",
         "refs": ["bead:auto-epic", "bead:auto-1"]}]}

    fake = _run(parser, ["mission", "update", MID, "relay", "c1",
                         "--ref", "bead:auto-2", "--ref", "bead:auto-1"],
                monkeypatch, canned)
    put = next(b for m, p2, b in fake.calls if m == "PUT")
    assert put["refs"] == ["bead:auto-epic", "bead:auto-1", "bead:auto-2"]

    fake = _run(parser, ["mission", "update", MID, "relay", "c1",
                         "--title", "new title"], monkeypatch, canned)
    put = next(b for m, p2, b in fake.calls if m == "PUT")
    assert put["refs"] == ["bead:auto-epic", "bead:auto-1"]

    fake = _run(parser, ["mission", "update", MID, "relay", "c1",
                         "--clear-refs", "--ref", "bead:auto-9"],
                monkeypatch, canned)
    put = next(b for m, p2, b in fake.calls if m == "PUT")
    assert put["refs"] == ["bead:auto-9"]


def test_retire_flags_item_and_coverage_ignores_it(parser, monkeypatch,
                                                  capsys):
    """The first legitimate retirement need: converging parallel
    bookings. Retire is a flag (record kept), restore undoes it, and a
    retired checkpoint stops counting toward coverage."""
    canned = dict(BASE)
    canned[("GET", f"/api/mission/items/{MID}")] = {"items": [
        {"surface_id": "relay", "item_id": "dup", "kind": "checkpoint",
         "state": "pending", "title": "duplicate", "key": "relay:dup",
         "created_at": "c", "updated_at": "u",
         "refs": ["bead:auto-1"]}]}
    fake = _run(parser, ["mission", "retire", MID, "relay", "dup"],
                monkeypatch, canned)
    put = next(b for m, p2, b in fake.calls if m == "PUT")
    assert put["retired"] is True
    assert put["title"] == "duplicate"          # full record survives

    fake = _run(parser, ["mission", "retire", MID, "relay", "dup",
                         "--restore"], monkeypatch, canned)
    put = next(b for m, p2, b in fake.calls if m == "PUT")
    assert put["retired"] is False

    # coverage: the retired checkpoint neither counts nor covers
    canned[("GET", f"/api/mission/items/{MID}")] = {"items": [
        {"surface_id": "relay", "item_id": "dup", "kind": "checkpoint",
         "state": "pending", "title": "duplicate", "retired": True,
         "refs": ["bead:auto-1"]}]}
    fake = _run(parser, ["mission", "coverage", MID], monkeypatch, canned)
    out = capsys.readouterr().out
    assert "0 criteria covering 0" in out


def test_items_json_carries_pillar_alias(parser, monkeypatch, capsys):
    canned = dict(BASE)
    canned[("GET", f"/api/mission/items/{MID}")] = {"items": [
        {"surface_id": "relay", "item_id": "c1", "kind": "checkpoint",
         "state": "pending", "title": "t"}]}
    _run(parser, ["mission", "items", MID, "--json"], monkeypatch, canned)
    out = json.loads(capsys.readouterr().out)
    assert out[0]["pillar"] == "relay"


def test_status_resolves_pillar_by_display_name_or_errors(
        parser, monkeypatch, capsys):
    """'graph mission status <m> \"Display Name\"' silently returned
    only the header; it now resolves the name to the slug, and an
    unknown pillar errors listing the real ones."""
    canned = dict(BASE)
    _run(parser, ["mission", "status", MID, "Relay"],
         monkeypatch, canned)
    out = capsys.readouterr().out
    assert "Relay" in out and "delivery" in out

    with pytest.raises(SystemExit):
        _run(parser, ["mission", "status", MID, "No Such Pillar"],
             monkeypatch, canned)
    err = capsys.readouterr().err
    assert "no pillar" in err and "relay" in err
