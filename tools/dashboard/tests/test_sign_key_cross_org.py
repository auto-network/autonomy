"""GET /api/sign-key serves the OPERATOR'S OWN signing key from personal.db.

auto-h4kzx (read-side leak fix, retained below at the primitive level): a
signing-key row that is ``canonical`` sits on an org's cross-org read-through
surface, so the old federated ``read_set`` served it to any subscribing org;
``read_owned_set`` excludes another org's row by construction.

auto-bsbaf (authority re-home): the commit-signing key is the operator's OWN
secret, so it lives in ``personal.db`` and get_sign_key now reads it pinned to
``personal`` (a legacy key still in the caller's own org DB is read owning-DB
only, transitionally, until moved). A personal secret is never in an org DB, so
it is structurally never on any org's cross-org read-through surface — the real
fix for the exposure, of which the owning-DB read was the symptom-level stop.
"""

from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.commit_signing_key import SIGN_KEY_SET_ID

ARMORED = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
)


@pytest.fixture
def orgs(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("anchore").close()
    GraphDB.create_org_db("beta").close()
    yield
    GraphDB.close_all_pooled()


def test_owned_set_excludes_another_orgs_canonical_sign_key(orgs):
    # anchore has a CANONICAL signing key — the federation-visible state the
    # live exposure was found in.
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "default",
        {"armored_private_key": ARMORED}, org="anchore", state="canonical",
    )
    # beta reading its OWN owned set sees nothing: anchore's canonical row lives
    # in anchore's DB and is never composed in. This is exactly what
    # get_sign_key now uses; the old federated read_set returned it cross-org.
    assert settings_ops.read_owned_set(SIGN_KEY_SET_ID, org="beta").members == []


def test_owned_set_still_serves_own_org_sign_key(orgs):
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "default",
        {"armored_private_key": ARMORED}, org="anchore", state="canonical",
    )
    own = settings_ops.read_owned_set(SIGN_KEY_SET_ID, org="anchore").members
    assert len(own) == 1
    assert own[0].payload["armored_private_key"] == ARMORED


def test_get_sign_key_serves_the_operators_personal_key(tmp_path, monkeypatch):
    """auto-bsbaf: the handler reads the operator's OWN key from personal.db,
    not from any org DB — a personal secret has no org to name."""
    import asyncio

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("personal", type_="personal").close()
    settings_ops.upsert_by_key(
        SIGN_KEY_SET_ID, 1, "default",
        {"armored_private_key": ARMORED}, org="personal", state="raw",
    )
    from tools.dashboard.approvals_routes import get_sign_key

    class _Req:
        query_params: dict = {}

    resp = asyncio.run(get_sign_key(_Req()))
    assert resp.status_code == 200
    assert "PRIVATE KEY" in resp.body.decode()
    GraphDB.close_all_pooled()


def test_get_sign_key_404_when_no_personal_key(tmp_path, monkeypatch):
    """No key anywhere -> 404, not an org-DB read."""
    import asyncio

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db("personal", type_="personal").close()
    from tools.dashboard.approvals_routes import get_sign_key

    class _Req:
        query_params: dict = {}

    resp = asyncio.run(get_sign_key(_Req()))
    assert resp.status_code == 404
    GraphDB.close_all_pooled()
