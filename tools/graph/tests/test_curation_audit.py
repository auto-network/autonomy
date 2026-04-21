"""Tests for the curation audit script."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph.curation import audit
from tools.graph.curation.allowlist import load as load_allowlist
from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


def _insert_note(db, *, id: str, title: str, tags: list[str], state: str = "raw"):
    src = Source(
        id=id,
        type="note",
        platform="local",
        project=None,
        title=title,
        file_path=f"note:{id}",
        metadata={"tags": tags, "author": "librarian"},
        publication_state=state,
    )
    db.insert_source(src)
    t = Thought(source_id=src.id, content=f"body of {title}", role="user", turn_number=1)
    db.insert_thought(t)
    db.insert_note_version(src.id, 1, f"body of {title}")
    return src


def _write_allowlist(path: Path, canonical: list[str], published: list[str]) -> Path:
    body = "org: autonomy\nversion: 1\ncanonical:\n"
    for p in canonical:
        body += f"  - {p}\n"
    body += "published:\n"
    for p in published:
        body += f"  - {p}\n"
    path.write_text(body)
    return path


@pytest.fixture
def db(tmp_path):
    d = GraphDB(tmp_path / "graph.db")
    yield d
    d.close()


def test_report_marks_allowlist_entries_with_tier(db, tmp_path):
    _insert_note(db, id="38c10838-094-aaa", title="Signpost", tags=["signpost"])
    _insert_note(db, id="bcce359d-a1d-bbb", title="Cross-Org Search", tags=["architecture"])
    allow = _write_allowlist(
        tmp_path / "a.yaml",
        canonical=["38c10838-094"],
        published=["bcce359d-a1d"],
    )
    report = audit.build_report(db=db, allowlist=load_allowlist(allow), db_path=str(tmp_path / "graph.db"))
    rows = {r.source_id: r for r in report.rows}
    assert rows["38c10838-094-aaa"].allowlist_tier == "canonical"
    assert rows["38c10838-094-aaa"].proposed_state == "canonical"
    assert rows["bcce359d-a1d-bbb"].allowlist_tier == "published"
    assert rows["bcce359d-a1d-bbb"].proposed_state == "published"


def test_report_flags_missing_allowlist_entry(db, tmp_path):
    # Allowlist names a note that isn't in the DB.
    allow = _write_allowlist(tmp_path / "a.yaml", canonical=["nosuch-00"], published=[])
    report = audit.build_report(db=db, allowlist=load_allowlist(allow), db_path="x")
    row = next(r for r in report.rows if r.source_id == "nosuch-00")
    assert row.missing is True
    assert row.allowlist_tier == "canonical"


def test_report_flags_ambiguous_prefix(db, tmp_path):
    _insert_note(db, id="dupprefix-aaa", title="A", tags=[])
    _insert_note(db, id="dupprefix-bbb", title="B", tags=[])
    allow = _write_allowlist(tmp_path / "a.yaml", canonical=["dupprefix-"], published=[])
    report = audit.build_report(db=db, allowlist=load_allowlist(allow), db_path="x")
    row = next(r for r in report.rows if r.source_id == "dupprefix-")
    assert row.ambiguous_prefix is True
    assert set(row.candidate_ids) == {"dupprefix-aaa", "dupprefix-bbb"}


def test_report_collects_seed_tag_candidates_not_in_allowlist(db, tmp_path):
    _insert_note(db, id="aaa", title="Has signpost tag", tags=["signpost"])
    _insert_note(db, id="bbb", title="Has architecture tag", tags=["architecture"])
    _insert_note(db, id="ccc", title="Has protocol tag", tags=["protocol"])
    _insert_note(db, id="ddd", title="Untagged note", tags=["other"])
    allow = _write_allowlist(tmp_path / "a.yaml", canonical=[], published=[])
    report = audit.build_report(db=db, allowlist=load_allowlist(allow), db_path="x")
    seed_ids = {r.source_id for r in report.rows if r.allowlist_tier is None}
    assert seed_ids == {"aaa", "bbb", "ccc"}


def test_report_counts_pending_comments(db, tmp_path):
    src = _insert_note(db, id="cmt-src", title="With comments", tags=["signpost"])
    db.insert_comment(src.id, "outstanding 1")
    db.insert_comment(src.id, "outstanding 2")
    c3 = db.insert_comment(src.id, "already integrated")
    db.integrate_comment(c3["id"])
    allow = _write_allowlist(tmp_path / "a.yaml", canonical=["cmt-src"], published=[])
    report = audit.build_report(db=db, allowlist=load_allowlist(allow), db_path="x")
    row = next(r for r in report.rows if r.source_id == "cmt-src")
    assert row.pending_comments == 2


def test_render_text_summary_flags_blocked_canonical(db, tmp_path):
    src = _insert_note(db, id="blk-src", title="Has pending comments", tags=["signpost"])
    db.insert_comment(src.id, "pending")
    allow = _write_allowlist(tmp_path / "a.yaml", canonical=["blk-src"], published=[])
    report = audit.build_report(db=db, allowlist=load_allowlist(allow), db_path="x")
    text = audit.render_text(report)
    assert "canonical candidates with pending comments" in text
    assert "pending_comments=1" in text


def test_cli_writes_json_output(db, tmp_path, monkeypatch):
    _insert_note(db, id="jcli-aaa", title="X", tags=["signpost"])
    allow = _write_allowlist(tmp_path / "a.yaml", canonical=["jcli-aaa"], published=[])
    out = tmp_path / "report.json"
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    rc = audit.run(["--allowlist", str(allow), "--output", str(out), "--json"])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["org"] == "autonomy"
    assert any(r["source_id"] == "jcli-aaa" for r in data["rows"])


def test_cli_text_output_to_stdout(db, tmp_path, monkeypatch, capsys):
    _insert_note(db, id="scli-aaa", title="Y", tags=["protocol"])
    allow = _write_allowlist(tmp_path / "a.yaml", canonical=["scli-aaa"], published=[])
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    rc = audit.run(["--allowlist", str(allow)])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "Bootstrap allowlist audit" in captured
    assert "scli-aaa" in captured
