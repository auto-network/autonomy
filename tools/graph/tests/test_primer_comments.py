"""L1 test for primer comment rendering.

Regression guard for auto-7l35: `_get_bead` previously shelled out to
`bd show --json`, which drops comments. The primer now reads via the
dashboard DAO, so every bead comment must reach the agent.
"""

from __future__ import annotations

import pytest

from tools.graph.primer import generate_primer


def test_primer_includes_comments(monkeypatch):
    """generate_primer(bead_with_comments) must contain every comment body.

    The bead comes from the dashboard DAO here rather than from the live
    Dolt server. Reading a real bead id made this an integration test
    wearing an L1 label: wherever the DAO cannot reach Dolt — this
    container reaches it from the `bd` CLI but not from the DAO, which
    dials 172.17.0.1:3306 — generate_primer returned "(Could not fetch
    bead details from bd)" and the assertions failed for a reason that
    has nothing to do with rendering.

    Stubbing the DAO does not weaken the regression it guards. If
    _get_bead went back to shelling out to `bd show --json`, the stub
    would not be consulted, the comments would not appear, and these
    assertions would fail — which is the whole point of auto-7l35.
    """
    from tools.dashboard.dao import beads as dao_beads

    bead = {
        "id": "auto-1v0o",
        "title": "Session viewer grid shell layout",
        "description": "the bead's own description",
        "status": "closed",
        "priority": 1,
        "comments": [
            {"author": "terminal:auto-0416-223035",
             "text": "Coordination channel for the grid shell work."},
            {"author": "terminal:auto-0416-223035",
             "text": "Test Coverage Audit: every part has an L1 guard."},
        ],
    }
    monkeypatch.setattr(dao_beads, "get_bead", lambda _id: bead)

    primer = generate_primer("auto-1v0o")
    assert "Coordination channel" in primer, \
        "post-description comments missing from primer"
    assert "Test Coverage Audit" in primer, \
        "test-audit comment missing from primer"
    assert "auto-0416-223035" in primer, \
        "coordination session id missing from primer"
