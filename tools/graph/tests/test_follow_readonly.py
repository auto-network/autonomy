"""Read-only enforcement and lifecycle for followed-org mirrors
(design of record graph://5f2f5a49-00d §10.4, bead auto-3534i).

A followed mirror (``orgs.type='followed'``) is a read-only cache of another
org's public surface. Every local write path refuses it with the typed
``FollowedOrgReadOnly``; reads work through the ordinary peer path; removal
drops the row and the mirror so ``cross_org.resolve_peers`` no longer lists it.
"""
from __future__ import annotations

import pytest

from tools.graph import cross_org, ops, settings_ops
from tools.graph.db import (
    FollowedOrgReadOnly, GraphDB, _ORG_TYPE_CACHE, _org_db_path,
    is_followed_org,
)

SLUG = "followedorg"
ORG_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def follow_env(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    _ORG_TYPE_CACHE.clear()
    # A local shared org, and a followed mirror beside it.
    GraphDB.create_org_db("localorg", type_="shared").close()
    GraphDB.create_org_db(SLUG, type_="followed", org_id=ORG_UUID).close()
    yield orgs_dir
    GraphDB.close_all_pooled()
    _ORG_TYPE_CACHE.clear()


def test_is_followed_org_detects_the_mirror(follow_env):
    assert is_followed_org(SLUG) is True
    assert is_followed_org("localorg") is False
    assert is_followed_org("nope") is False


def test_content_write_into_a_mirror_is_refused(follow_env):
    with pytest.raises(FollowedOrgReadOnly) as exc:
        ops._open(SLUG)
    assert exc.value.slug == SLUG


def test_settings_write_into_a_mirror_is_refused(follow_env):
    with pytest.raises(FollowedOrgReadOnly):
        settings_ops._open(SLUG, "autonomy.workspace", for_read=False)
    # A read is never refused.
    db = settings_ops._open(SLUG, "autonomy.workspace", for_read=True)
    db.close()


def test_for_org_refuses_write_mode_but_allows_read(follow_env):
    with pytest.raises(FollowedOrgReadOnly):
        GraphDB.for_org(SLUG, mode="rw")
    db = GraphDB.for_org(SLUG, mode="ro")
    assert db.conn.execute("SELECT type FROM orgs").fetchone()[0] == "followed"


def test_session_scoping_to_a_mirror_is_refused(follow_env):
    with pytest.raises(FollowedOrgReadOnly):
        ops.set_caller_org(SLUG)
    # A non-followed org scopes normally.
    token = ops.set_caller_org("localorg")
    ops.reset_caller_org(token)


def test_a_mirror_is_a_read_peer(follow_env):
    peers = cross_org.resolve_peers("localorg", None)
    assert SLUG in peers


def test_remove_drops_the_row_and_mirror(follow_env, monkeypatch):
    from tools.graph.schemas.org_follow import (
        ORG_FOLLOW_REVISION, ORG_FOLLOW_SET_ID,
    )
    from tools.graph import follow_cmd

    # A follow row for the mirror.
    settings_ops.add_setting(
        ORG_FOLLOW_SET_ID, ORG_FOLLOW_REVISION, SLUG,
        {"org_uuid": ORG_UUID, "rendezvous": "https://relay/l/tok",
         "link_pub": "ab" * 32, "enabled": True,
         "added_at": "2026-09-22T00:00:00Z"},
        org=None,
    )
    assert (_org_db_path(SLUG)).exists()

    class _Args:
        target = SLUG
        keep_mirror = False

    follow_cmd.cmd_follow_remove(_Args())

    # Mirror file gone → no longer a peer.
    assert not (_org_db_path(SLUG)).exists()
    peers = cross_org.resolve_peers("localorg", None)
    assert SLUG not in peers
    # The follow row is dropped from the enabled set.
    rows = follow_cmd._follow_rows()
    assert all(m.key != SLUG for m in rows)


def test_remove_keep_mirror_leaves_the_readable_cache(follow_env):
    from tools.graph.schemas.org_follow import (
        ORG_FOLLOW_REVISION, ORG_FOLLOW_SET_ID,
    )
    from tools.graph import follow_cmd

    settings_ops.add_setting(
        ORG_FOLLOW_SET_ID, ORG_FOLLOW_REVISION, SLUG,
        {"org_uuid": ORG_UUID, "rendezvous": "https://relay/l/tok",
         "link_pub": "ab" * 32, "enabled": True,
         "added_at": "2026-09-22T00:00:00Z"},
        org=None,
    )

    class _Args:
        target = SLUG
        keep_mirror = True

    follow_cmd.cmd_follow_remove(_Args())
    assert (_org_db_path(SLUG)).exists()
    assert SLUG in cross_org.resolve_peers("localorg", None)
