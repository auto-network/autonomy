"""``GET /api/attachment/<id>`` serves an organization attachment that ARRIVED
BY SYNC (operator ruling 2026-09-13: the photo is the attachment).

Fleet sync replicates the ``attachments`` row from another member's machine
and installs the bytes content-addressed under the store's
``uploads/fleet/<hash[:2]>/`` — a different root from the upload store this
process writes, and a ``file_path`` recorded in the WRITER's frame. The
route must resolve such a row to the local bytes, with or without an
explicit ``?org=`` naming the store, and must never create a store for an
organization this machine does not have.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.graph import ops as graph_ops
from tools.graph.db import GraphDB
from tools.graph.models import Attachment

ORG = "acme"
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 synced-profile-photo"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs_dir / "personal.db").close()
    db = GraphDB.create_org_db(ORG, type_="shared", path=orgs_dir / f"{ORG}.db")
    # A row exactly as materialize() installs it: bytes under the fleet blob
    # root, the writer's own (foreign) frame path never valid here.
    digest = hashlib.sha256(WEBP).hexdigest()
    blob = orgs_dir / "uploads" / "fleet" / digest[:2] / f"{digest}.webp"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(WEBP)
    att = Attachment(hash=digest, filename="profile-avatar.webp", mime_type="image/webp",
                     size_bytes=len(WEBP), file_path="/app/data/attachments/xx/other-machine.webp")
    db.insert_attachment(att)
    db.close()
    from tools.dashboard import server

    app = Starlette(routes=[Route("/api/attachment/{attachment_id}", server.api_attachment_serve)])
    with TestClient(app) as client:
        client.att_id = att.id  # type: ignore[attr-defined]
        yield client
    GraphDB.close_all_pooled()


def test_synced_org_attachment_resolves_to_the_fleet_blob(env):
    att = graph_ops.get_attachment(env.att_id, org=ORG, peers=[])
    assert att is not None
    assert Path(att["file_path"]).read_bytes() == WEBP
    assert "uploads/fleet" in att["file_path"]


def test_route_serves_it_with_the_store_named(env):
    r = env.get(f"/api/attachment/{env.att_id}?org={ORG}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/webp")
    assert r.content == WEBP


def test_route_serves_it_scopeless(env):
    r = env.get(f"/api/attachment/{env.att_id}")
    assert r.status_code == 200, r.text
    assert r.content == WEBP


def test_wrong_store_and_unknown_store_are_not_found(env, tmp_path):
    assert env.get(f"/api/attachment/{env.att_id}?org=personal").status_code == 404
    assert env.get(f"/api/attachment/{env.att_id}?org=nope").status_code == 404
    # Naming a store this machine does not have never creates one.
    assert not (tmp_path / "orgs" / "nope.db").exists()
