"""The mission curator's primer: payload-driven, validated at build time.

The primer is the whole targeting mechanism — a wrong mission id or a
missing report channel must fail when the job is built (the API preflights
exactly this), never inside a running container.
"""
from __future__ import annotations

import pytest

from tools.dashboard.dao import mission_control_db as db
from agents.librarians.mission_curator.primer import build_primer


@pytest.fixture()
def mission(tmp_path, monkeypatch):
    path = tmp_path / "mc.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    m = db.create_mission("Curate Me")
    db.create_pillar(m["mission_id"], "Alpha", "sess-a", "#aaa")
    db.create_pillar(m["mission_id"], "Beta", "sess-b", "#bbb")
    return m


def test_requires_mission_and_report_to(mission):
    with pytest.raises(ValueError, match="mission_id"):
        build_primer({"report_to": "x"})
    with pytest.raises(ValueError, match="report_to"):
        build_primer({"mission_id": mission["mission_id"]})


def test_unknown_mission_fails_loudly(mission):
    with pytest.raises(ValueError, match="unknown mission"):
        build_primer({"mission_id": "nope", "report_to": "auto-x"})


def test_enumerates_overview_and_every_pillar(mission):
    text = build_primer({"mission_id": mission["mission_id"],
                         "report_to": "auto-x"})
    assert mission["mission_id"] in text
    assert "Alpha" in text and "Beta" in text
    # The pass-1 lesson, pinned: scope by inventory, never by noun.
    assert "ALL of them" in text


def test_pillar_filter_narrows_and_bad_filter_fails(mission):
    text = build_primer({"mission_id": mission["mission_id"],
                         "report_to": "auto-x", "pillars": ["alp"]})
    assert "Alpha" in text and "Beta" not in text
    with pytest.raises(ValueError, match="match nothing"):
        build_primer({"mission_id": mission["mission_id"],
                      "report_to": "auto-x", "pillars": ["gamma"]})


def test_dry_run_and_budget_modes(mission):
    dry = build_primer({"mission_id": mission["mission_id"],
                        "report_to": "auto-x", "dry_run": True})
    assert "DRY RUN" in dry and "ZERO write" in dry
    live = build_primer({"mission_id": mission["mission_id"],
                         "report_to": "auto-x", "write_budget": 3})
    assert "MODE: LIVE" in live and "at most 3 write actions" in live


def test_registered_with_dispatcher_and_template_appended(mission):
    from agents.dispatcher import _build_librarian_prompt
    full = _build_librarian_prompt("mission_curate", {
        "mission_id": mission["mission_id"], "report_to": "auto-x",
        "dry_run": True,
    })
    assert "Run scope — mission curation pass" in full     # dynamic primer
    assert "# Mission Curator" in full                     # static template
    assert "results.json" in full                          # output contract
