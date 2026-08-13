"""auto-h4kzx — GET /api/sign-key must serve only the CALLER'S OWN org signing
key. A signing-key row that is ``canonical`` (a real state found live in the
fleet: anchore's row) is on the cross-org read-through surface, so the old
federated ``read_set`` served it to any subscribing org. The route now reads
the owning DB only (``read_owned_set``), which excludes another org's row —
canonical or not — by construction.
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
