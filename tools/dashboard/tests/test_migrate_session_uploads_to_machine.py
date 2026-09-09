"""The upload migration claims a machine's own rows and nothing else.

The interesting property is not "it copies rows" — it is that attribution is
decided by the FILE, since the row carries no machine identity. A tool that got
this wrong would claim every row on the first machine that ran it and leave the
other machine's uploads pointing at a store that does not have them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.dashboard import migrate_session_uploads_to_machine as mig


def _payload(session="auto-0101-000000", rel=".uploads/shot.png"):
    return {"target_session": session, "filename": "shot.png",
            "rel_path": rel, "mime": "image/png", "size": 3,
            "timestamp": "2026-01-01T00:00:00Z"}


def test_a_row_whose_file_is_here_is_claimed(tmp_path):
    run = tmp_path / "auto-0101-000000-20260101" / ".uploads"
    run.mkdir(parents=True)
    (run / "shot.png").write_bytes(b"png")

    assert mig._file_is_here(tmp_path, _payload()) == run / "shot.png"


def test_a_row_whose_file_is_elsewhere_is_left_alone(tmp_path):
    """THE ONE THAT MATTERS. This machine must not claim another machine's
    upload: the row would then live in a machine store whose disk cannot serve
    it, which is the same falsely-present tile the schema change removed."""
    (tmp_path / "auto-0101-000000").mkdir()

    assert mig._file_is_here(tmp_path, _payload()) is None


def test_the_bare_run_dir_is_matched_as_well_as_timestamped_ones(tmp_path):
    run = tmp_path / "auto-0101-000000" / ".uploads"
    run.mkdir(parents=True)
    (run / "shot.png").write_bytes(b"png")

    assert mig._file_is_here(tmp_path, _payload()) == run / "shot.png"


def test_a_similarly_named_session_is_not_matched(tmp_path):
    """`startswith` on a bare name would match `auto-0101-0000001`; the dash is
    what makes the prefix a run-dir stamp rather than a longer session name."""
    run = tmp_path / "auto-0101-0000009" / ".uploads"
    run.mkdir(parents=True)
    (run / "shot.png").write_bytes(b"png")

    assert mig._file_is_here(tmp_path, _payload()) is None


@pytest.mark.parametrize("rel", ["/etc/passwd", "../../etc/passwd",
                                 ".uploads/../../../etc/passwd"])
def test_an_escaping_rel_path_is_refused_not_resolved(tmp_path, rel):
    """`rel_path` is relative by contract. A row that violates it is refused
    here rather than resolved — this tool walks the filesystem on the strength
    of a stored string."""
    assert mig._file_is_here(tmp_path, _payload(rel=rel)) is None


def test_an_incomplete_row_is_refused(tmp_path):
    assert mig._file_is_here(tmp_path, {}) is None
    assert mig._file_is_here(tmp_path, _payload(session="")) is None
    assert mig._file_is_here(tmp_path, _payload(rel="")) is None


def test_a_missing_agent_runs_root_reports_nothing_rather_than_raising(tmp_path):
    assert mig._run_dirs(tmp_path / "absent", "auto-0101-000000") == []


# ── Refusing to look successful (host-0906-222509, 2026-09-09) ──────────
#
# Both failures below produced "0 of 0 rows" on the first real run, which is
# exactly what a COMPLETED migration prints. Each test asserts the tool now
# refuses instead, because a silent no-op here does not merely fail — it tells
# the next reader the work is done.


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_an_agent_runs_root_it_cannot_see_is_refused(tmp_path, monkeypatch, state):
    """THE ONE THAT MATTERS. On the host, agent-runs resolves to a directory
    that EXISTS and is EMPTY while the real store (2991 run dirs) is the
    dashboard container's. Every row would have been reported as belonging to
    another machine — a false negative that looks like a clean result."""
    root = tmp_path / "agent-runs"
    if state == "empty":
        root.mkdir()
    monkeypatch.setattr(
        "tools.dashboard.session_monitor._agent_runs_root", lambda: root)

    with pytest.raises(mig.MigrationRefused) as exc:
        mig.migrate("autonomy", apply=False)

    assert str(root) in str(exc.value)


def _seed(orgs, key, payload, *, deprecated=0, supersedes=None, excludes=None):
    """Insert a legacy row directly: `_open` would refuse this now, which is
    exactly why these rows are stranded."""
    import json
    import uuid

    from tools.graph.db import GraphDB

    db = GraphDB(orgs / "autonomy.db")
    try:
        db.conn.execute(
            "INSERT INTO settings (id, set_id, schema_revision, key, payload,"
            " publication_state, deprecated, supersedes, excludes)"
            " VALUES (?, ?, 1, ?, ?, 'raw', ?, ?, ?)",
            (str(uuid.uuid4()), mig.SESSION_UPLOAD_SET_ID, key,
             json.dumps(payload), deprecated, supersedes, excludes),
        )
        db.conn.commit()
    finally:
        db.close()


@pytest.fixture
def org_store(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("autonomy").close()
    yield orgs
    GraphDB.close_all_pooled()


def test_the_count_gap_is_named_and_classified(org_store):
    """A raw `count(*)` and this tool's total differ by the non-base rows, and
    the difference was noticed by hand on the first real run (107 vs 106).
    Three things land there and only ONE is safe to ignore, so the tool reports
    the classification rather than a number."""
    _seed(org_store, "live", _payload())
    _seed(org_store, "gone", _payload(), deprecated=1)
    _seed(org_store, "patched", _payload(), supersedes="some-base-id")
    _seed(org_store, "removed", _payload(), excludes="some-base-id")

    assert [k for k, _ in mig.legacy_rows("autonomy")] == ["live"]
    assert {e["key"]: e["kind"] for e in mig.non_base_rows("autonomy")} == {
        "gone": "deprecated", "patched": "override", "removed": "exclusion",
    }


def test_applying_a_key_whose_value_is_not_its_base_is_refused(
    org_store, tmp_path, monkeypatch,
):
    """THE ONE THAT MATTERS for --apply. This tool copies BASE rows. When a key
    also has an override, the value in force is base + patch, so copying the
    base migrates a STALE PAYLOAD — one wrong row inside an otherwise correct
    migration, which is where it would never be found. Refuse the run."""
    _seed(org_store, "patched", _payload())
    _seed(org_store, "patched", _payload(), supersedes="some-base-id")
    root = tmp_path / "agent-runs"
    (root / "auto-0101-000000" / ".uploads").mkdir(parents=True)
    (root / "auto-0101-000000" / ".uploads" / "shot.png").write_bytes(b"png")
    monkeypatch.setattr(
        "tools.dashboard.session_monitor._agent_runs_root", lambda: root)

    # The dry run is still allowed to report it — refusing to LOOK would hide
    # the very thing an operator needs to see before deciding.
    dry = mig.migrate("autonomy", apply=False)
    assert dry["claimed"] == ["patched"]

    with pytest.raises(mig.MigrationRefused) as exc:
        mig.migrate("autonomy", apply=True)
    assert "patched" in str(exc.value)


def test_enumeration_sees_rows_the_ordinary_read_cannot(tmp_path, monkeypatch):
    """THE BUG THIS TOOL SHIPPED WITH. `read_set` resolves the store through
    the schema's declared home, which is now `machine` — so the ordinary read
    returns zero for the org-homed rows the tool exists to move, and `--apply`
    was a silent no-op. Enumeration must go past that redirect.

    Asserted as a CONTRAST: the ordinary read sees nothing, this one sees the
    row. Asserting only the second would pass against a tool that had never
    had the bug and against one where the redirect had quietly stopped
    applying."""
    from tools.graph import settings_ops as ops
    from tools.graph.db import GraphDB

    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("autonomy").close()

    # Seed the legacy row the way it exists today: in the ORG store, written
    # before the home changed. `_open` would refuse this now, which is the
    # whole reason these rows are stranded — so write it directly.
    import json
    import uuid

    payload = _payload()
    key = "row-legacy"
    db = GraphDB(orgs / "autonomy.db")
    try:
        db.conn.execute(
            "INSERT INTO settings (id, set_id, schema_revision, key, payload,"
            " publication_state, deprecated) VALUES (?, ?, 1, ?, ?, 'raw', 0)",
            (str(uuid.uuid4()), mig.SESSION_UPLOAD_SET_ID, key,
             json.dumps(payload)),
        )
        db.conn.commit()
    finally:
        db.close()

    assert ops.read_set(mig.SESSION_UPLOAD_SET_ID, org="autonomy").members == []
    assert mig.legacy_rows("autonomy") == [(key, payload)]

    GraphDB.close_all_pooled()
