"""Org-scoping for Design Studio: the caller's bearer org is authoritative.

Two layers under test:

* ``api_auth.caller_org_scope_hides`` — the canonical org-scoped-resource
  visibility predicate the design routes (and the session routes) share. An
  org-bound caller sees only its own org; a global-authority caller sees all;
  an unattributable resource is hidden from an org caller.
* ``design_db`` — a design carries an ``org`` stamped at create, a new revision
  inherits its design's org, and recently-modified NULL-org designs are
  backfilled to the obvious org while older ones stay NULL.
"""

from __future__ import annotations

import sqlite3

import pytest
from starlette.requests import Request

from tools.dashboard import api_auth


def _request(principal) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/design/x/full",
        "query_string": b"",
        "headers": [],
        "state": {"api_principal": principal},
    })


ORG = api_auth.ApiPrincipal(
    api_auth.ApiPrincipalKind.ORG_SESSION, subject="agent", org="autonomy")
ORG_NO_SLUG = api_auth.ApiPrincipal(
    api_auth.ApiPrincipalKind.ORG_SESSION, subject="agent", org=None)
LOCAL = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.LOCAL_SESSION, subject="host")
OPERATOR = api_auth.ApiPrincipal(api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="op")


def test_org_caller_hidden_from_another_orgs_design():
    assert api_auth.caller_org_scope_hides(_request(ORG), "anchore") is True


def test_org_caller_sees_its_own_orgs_design():
    assert api_auth.caller_org_scope_hides(_request(ORG), "autonomy") is False


def test_unattributable_design_hidden_from_org_caller():
    assert api_auth.caller_org_scope_hides(_request(ORG), None) is True


def test_org_caller_without_a_slug_is_hidden():
    assert api_auth.caller_org_scope_hides(_request(ORG_NO_SLUG), "autonomy") is True


def test_global_authority_sees_every_org():
    assert api_auth.caller_org_scope_hides(_request(LOCAL), "anchore") is False
    assert api_auth.caller_org_scope_hides(_request(OPERATOR), "anchore") is False
    assert api_auth.caller_org_scope_hides(_request(LOCAL), None) is False


def test_compatibility_traffic_is_not_judged():
    assert api_auth.caller_org_scope_hides(
        _request(api_auth.COMPATIBILITY_PRINCIPAL), "anchore") is False


# ── design_db org column: stamp, inherit, backfill ────────────────────────

@pytest.fixture
def design_db(tmp_path, monkeypatch):
    import agents.design_db as ddb
    monkeypatch.setattr(ddb, "DB_PATH", tmp_path / "design.db")
    monkeypatch.setattr(ddb, "_initialized", False)
    yield ddb


def test_create_stamps_org_and_get_returns_it(design_db):
    rev = design_db.create_design(
        title="A", variants=[{"id": "main", "html": "<p>a</p>"}], org="autonomy")
    got = design_db.get_design(rev)
    assert got["org"] == "autonomy"


def test_a_new_revision_inherits_its_designs_org(design_db):
    first = design_db.create_design(
        title="A", variants=[{"id": "main", "html": "<p>1</p>"}], org="autonomy")
    did = design_db.get_design(first)["design_id"]
    # A later revision passing a DIFFERENT org still inherits the design's org.
    second = design_db.create_design(
        title="A", variants=[{"id": "main", "html": "<p>2</p>"}],
        design_id=did, org="anchore", force=True)
    assert design_db.get_design(second)["org"] == "autonomy"


def test_backfill_stamps_recent_null_org_and_leaves_old_null(design_db):
    # Build a pre-migration designs table (no org column) directly, with one
    # recent and one old design, then run the migration helper.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE designs (id TEXT PRIMARY KEY, design_id TEXT, "
        "revision_seq INTEGER, created_at TEXT)")
    conn.execute(
        "INSERT INTO designs VALUES ('r1','d_recent',1, datetime('now','-1 hours'))")
    conn.execute(
        "INSERT INTO designs VALUES ('r2','d_old',1, datetime('now','-30 days'))")
    conn.commit()

    design_db._ensure_org_column(conn)

    recent = conn.execute("SELECT org FROM designs WHERE id='r1'").fetchone()["org"]
    old = conn.execute("SELECT org FROM designs WHERE id='r2'").fetchone()["org"]
    assert recent == design_db._OBVIOUS_ORG
    assert old is None
