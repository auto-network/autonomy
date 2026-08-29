"""Duplicate-note guard on ``ops.create_note`` (auto-wfq7l Fix 2).

A create whose body is near-identical to a recently created note refuses
with :class:`ops.DuplicateNoteError` naming the existing id — the backstop
behind the CLI's deterministic quoted-subcommand guard, for the author who
never reached for ``update`` at all. Threshold and margins were measured
on the 2026-08-29 four-id incident corpus (auto-0828-134703): revisions
0.74-0.93, distinct notes <=0.073, default 0.70.
"""

from __future__ import annotations

import pytest

from tools.graph import ops
from tools.graph.db import GraphDB


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    try:
        yield root
    finally:
        GraphDB.close_all_pooled()


# ~600 chars normalized — comfortably past the 280-char floor.
_BODY = (
    "Dispatch overhaul findings, revision one. The queue drainer promotes "
    "QUEUED rows to PREPARING oldest-first, occupancy counts PREPARING plus "
    "RUNNING, and the only hard rejection left is the runaway ceiling at two "
    "hundred queued rows. Excess dispatches join the approved-waiting section "
    "and launch as slots free, which means the cap governs concurrency and "
    "never availability. The orphan sweep runs on the five minute watcher "
    "timer and fails RUNNING rows whose container is gone, replacing the old "
    "wedge guard that blocked launches for an hour after a crash. Evidence "
    "and timing tables are attached in the run transcript for the burst test."
)


def test_near_duplicate_create_is_refused(orgs_root):
    first = ops.create_note(_BODY)
    revised = _BODY.replace("revision one", "revision two").replace(
        "five minute", "ten minute")
    with pytest.raises(ops.DuplicateNoteError) as exc:
        ops.create_note(revised)
    assert exc.value.similar_id == first["id"]
    assert exc.value.similarity >= 0.70
    msg = str(exc.value)
    assert f"graph note update {first['id'][:12]}" in msg
    assert "--force" in msg


def test_force_bypasses_the_guard(orgs_root):
    ops.create_note(_BODY)
    result = ops.create_note(_BODY + " Appended clarification.", force=True)
    assert result["id"]


def test_short_bodies_are_exempt(orgs_root):
    ops.create_note("Deploy done for service A")
    result = ops.create_note("Deploy done for service B")
    assert result["id"]


def test_distinct_long_bodies_both_create(orgs_root):
    ops.create_note(_BODY)
    other = (
        "Design Studio fixture states for the harness usage strip. Each "
        "account renders one row per rate limit window, windows classify by "
        "duration rather than list position, and the stale badge appears only "
        "once a window's declared reset time has elapsed. Accounts are never "
        "hidden: an errored poll renders the last known values with the error "
        "chip rather than dropping the row, and the sort is stable on harness "
        "then identity so rows do not jump between refreshes. Responsive "
        "breakpoints collapse the sparkline first, then the identity column."
    )
    result = ops.create_note(other)
    assert result["id"]


def test_withdrawn_note_does_not_block(orgs_root):
    first = ops.create_note(_BODY)
    ops.withdraw_note(first["id"])
    result = ops.create_note(_BODY.replace("revision one", "revision three"))
    assert result["id"]
