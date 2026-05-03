"""Tests for the card_summary server-side helpers (auto-56o4m).

Covers ``_walk_decision_path`` (dotted-path resolver),
``_resolve_card_summary_slots`` (slot-list resolver), and
``_load_decision_for_run`` (decision.json loader).

The bigger ``_enrich_timeline_agentic`` integration is tested through the
existing timeline behavioural sweep — these unit tests focus on the
declarative path/slot vocabulary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.dashboard import server


def test_walk_decision_path_basic_dict():
    d = {"primary_outcome": "diamond"}
    assert server._walk_decision_path(d, "primary_outcome") == "diamond"


def test_walk_decision_path_nested_dict():
    d = {"quality_scores": {"architecture_fit": 3}}
    assert server._walk_decision_path(d, "quality_scores.architecture_fit") == 3


def test_walk_decision_path_missing_intermediate():
    d = {"a": {"b": "x"}}
    assert server._walk_decision_path(d, "a.c.d") is None


def test_walk_decision_path_none_input():
    assert server._walk_decision_path(None, "anything") is None


def test_walk_decision_path_list_index():
    d = {"items": [{"name": "first"}, {"name": "second"}]}
    assert server._walk_decision_path(d, "items.1.name") == "second"


def test_walk_decision_path_through_non_dict():
    d = {"a": "string-value"}
    # "a.b" past a string short-circuits to None instead of raising
    assert server._walk_decision_path(d, "a.b") is None


def test_resolve_card_summary_slots_full_round_trip():
    decision = {
        "primary_outcome": "refresh",
        "categorical": {"dominant_issue": "underspecified"},
        "quality_scores": {"architecture_fit": 3, "spec_clarity": 2},
        "summary": {"top_risk": "spec ambiguity"},
    }
    slots = [
        {"label": "Verdict", "path": "primary_outcome", "format": "badge"},
        {"label": "Issue", "path": "categorical.dominant_issue"},
        {"label": "Architecture fit",
         "path": "quality_scores.architecture_fit", "format": "stars"},
        {"label": "Top risk", "path": "summary.top_risk"},
    ]
    out = server._resolve_card_summary_slots(slots, decision)
    assert len(out) == 4
    assert out[0] == {"label": "Verdict", "value": "refresh", "format": "badge"}
    assert out[1] == {
        "label": "Issue", "value": "underspecified", "format": "text",
    }
    assert out[2]["value"] == 3
    assert out[2]["format"] == "stars"
    assert out[3]["value"] == "spec ambiguity"


def test_resolve_card_summary_slots_drops_missing_paths():
    decision = {"primary_outcome": "refresh"}
    slots = [
        {"label": "Verdict", "path": "primary_outcome"},
        {"label": "Missing", "path": "categorical.dominant_issue"},
    ]
    out = server._resolve_card_summary_slots(slots, decision)
    assert [s["label"] for s in out] == ["Verdict"]


def test_resolve_card_summary_slots_drops_empty_string_value():
    decision = {"summary": {"top_risk": ""}}
    slots = [{"label": "Top risk", "path": "summary.top_risk"}]
    assert server._resolve_card_summary_slots(slots, decision) == []


def test_resolve_card_summary_slots_no_decision():
    slots = [{"label": "Verdict", "path": "primary_outcome"}]
    assert server._resolve_card_summary_slots(slots, None) == []


def test_resolve_card_summary_slots_empty_slots():
    assert server._resolve_card_summary_slots([], {"x": 1}) == []
    assert server._resolve_card_summary_slots(None, {"x": 1}) == []


def test_resolve_card_summary_slots_skips_malformed_entries():
    decision = {"primary_outcome": "refresh"}
    slots = [
        "not-a-dict",  # ignored
        {"label": "OK", "path": "primary_outcome"},
        {"label": "", "path": "primary_outcome"},  # empty label dropped
        {"path": "primary_outcome"},  # missing label dropped
        {"label": "noPath"},  # missing path dropped
    ]
    out = server._resolve_card_summary_slots(slots, decision)
    assert [s["label"] for s in out] == ["OK"]


def test_resolve_card_summary_slots_default_format_is_text():
    decision = {"primary_outcome": "refresh"}
    slots = [{"label": "Verdict", "path": "primary_outcome"}]
    out = server._resolve_card_summary_slots(slots, decision)
    assert out[0]["format"] == "text"


def test_load_decision_for_run_reads_from_disk(tmp_path: Path):
    out_dir = tmp_path / "agentic-run-1234"
    out_dir.mkdir()
    decision = {"status": "DONE", "reason": "ok", "primary_outcome": "diamond"}
    (out_dir / "decision.json").write_text(json.dumps(decision))
    assert server._load_decision_for_run(str(out_dir)) == decision


def test_load_decision_for_run_missing_returns_none(tmp_path: Path):
    out_dir = tmp_path / "no-decision"
    out_dir.mkdir()
    assert server._load_decision_for_run(str(out_dir)) is None


def test_load_decision_for_run_empty_path_returns_none():
    assert server._load_decision_for_run("") is None
    assert server._load_decision_for_run(None) is None


def test_load_decision_for_run_malformed_json_returns_none(tmp_path: Path):
    out_dir = tmp_path / "bad-json"
    out_dir.mkdir()
    (out_dir / "decision.json").write_text("not json {")
    assert server._load_decision_for_run(str(out_dir)) is None


def test_load_decision_for_run_non_dict_returns_none(tmp_path: Path):
    out_dir = tmp_path / "list-not-dict"
    out_dir.mkdir()
    (out_dir / "decision.json").write_text(json.dumps([1, 2, 3]))
    assert server._load_decision_for_run(str(out_dir)) is None


def test_enrich_timeline_agentic_attaches_card_summary(tmp_path: Path, monkeypatch):
    """End-to-end: enricher reads decision.json, walks per-action card_summary
    paths, and attaches resolved slots to the entry."""
    out_dir = tmp_path / "agentic-run-7777"
    out_dir.mkdir()
    decision = {
        "status": "DONE",
        "reason": "audit complete",
        "primary_outcome": "refresh",
        "categorical": {"dominant_issue": "underspecified"},
        "quality_scores": {"architecture_fit": 3},
        "summary": {"top_risk": "spec ambiguity"},
    }
    (out_dir / "decision.json").write_text(json.dumps(decision))

    # Fake identity resolver — keep _enrich_timeline_agentic from hitting
    # the real graph DB. Returns a known member_key/target_org pair.
    monkeypatch.setattr(server, "_resolve_agentic_identity", lambda sid: {
        "action_label": "Dry-Run Implement",
        "member_key": "bead.dry-run-implement",
        "target_kind": "bead",
        "target_source_id": "auto-tv0x0",
        "target_org": "autonomy",
        "dispatched_by_session": "dashboard",
        "title": "Some bead title",
    })

    # Fake the action-member loader so we don't need a real settings DB.
    fake_slots = [
        {"label": "Verdict", "path": "primary_outcome", "format": "badge"},
        {"label": "Issue", "path": "categorical.dominant_issue"},
        {"label": "Architecture fit",
         "path": "quality_scores.architecture_fit", "format": "stars"},
        {"label": "Top risk", "path": "summary.top_risk"},
    ]
    monkeypatch.setattr(
        server,
        "_load_action_card_summaries",
        lambda pairs: {("bead.dry-run-implement", "autonomy"): fake_slots},
    )

    entries = [{
        "kind": "agentic",
        "agentic_source_id": "src-abc",
        "_output_dir": str(out_dir),
        "title": "",
    }]
    server._enrich_timeline_agentic(entries)

    cs = entries[0].get("card_summary")
    assert isinstance(cs, list) and len(cs) == 4
    assert cs[0] == {"label": "Verdict", "value": "refresh", "format": "badge"}
    assert cs[2]["value"] == 3
    assert cs[2]["format"] == "stars"


def test_enrich_timeline_agentic_no_slots_yields_empty_list(monkeypatch):
    """When the action member has no card_summary, the entry gets an empty
    list — not a missing key — so the template's x-show check is reliable."""
    monkeypatch.setattr(server, "_resolve_agentic_identity", lambda sid: {
        "action_label": "Some action",
        "member_key": "note.summarize",
        "target_kind": "note",
        "target_source_id": "src-1",
        "target_org": "autonomy",
        "dispatched_by_session": "dashboard",
        "title": "",
    })
    monkeypatch.setattr(
        server, "_load_action_card_summaries", lambda pairs: {}
    )
    entries = [{
        "kind": "agentic",
        "agentic_source_id": "src-abc",
        "_output_dir": "",
        "title": "",
    }]
    server._enrich_timeline_agentic(entries)
    assert entries[0]["card_summary"] == []


def test_enrich_timeline_agentic_skips_non_agentic_entries(monkeypatch):
    """Bead/librarian rows must not get a card_summary key, so the API
    payload stays unchanged for non-agentic kinds."""
    monkeypatch.setattr(
        server, "_load_action_card_summaries", lambda pairs: {}
    )
    entries = [
        {"kind": "bead", "agentic_source_id": None, "title": "x"},
        {"kind": "librarian", "agentic_source_id": None, "title": "y"},
    ]
    server._enrich_timeline_agentic(entries)
    for e in entries:
        assert "card_summary" not in e
