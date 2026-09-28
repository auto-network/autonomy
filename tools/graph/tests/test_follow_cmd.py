"""`graph follow add`: naming the local mirror (bead auto-krbtk, auto-iyxik;
design of record graph://5f2f5a49-00d §10.4/§10.8, D7).

The follower mirror lives at ``data/orgs/<slug>.db``. The slug an envelope
names is the publisher's claim: the mirror is named by
``org_ops.local_slug_for_org`` (keyed by uuid, disambiguated against a
different local org, reserved names skipped). An explicit ``--as`` is the
operator's own name: taken exactly, and refused up front, naming BOTH ids,
when a local organization of a DIFFERENT id already owns it.
"""

from __future__ import annotations

import argparse

import pytest

from tools.graph import follow_cmd
from tools.graph.db import GraphDB, _ORG_TYPE_CACHE


@pytest.fixture
def orgs_env(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    _ORG_TYPE_CACHE.clear()
    yield orgs_dir
    GraphDB.close_all_pooled()
    _ORG_TYPE_CACHE.clear()


def _envelope(org_uuid, slug):
    return {
        "target_type": "org:follow",
        "org": org_uuid,
        "meta": {"org": slug},
    }


def test_local_org_id_reads_the_orgs_row(orgs_env):
    GraphDB.create_org_db("acme", type_="shared", org_id="local-acme-id").close()
    assert follow_cmd._local_org_id("acme") == "local-acme-id"
    assert follow_cmd._local_org_id("nonesuch") is None


def _stop_at_the_row_write(monkeypatch):
    written = {}

    def stop(set_id, rev, key, payload, org=None):
        written.update({"key": key, "payload": payload})
        raise RuntimeError("stop here")

    from tools.graph import settings_ops
    monkeypatch.setattr(settings_ops, "add_setting", stop)
    return written


def test_a_published_slug_held_by_another_org_gets_the_uuid_suffix(orgs_env, monkeypatch):
    """The envelope's slug is the PUBLISHER's claim (auto-iyxik). A local org
    "acme" with a different id keeps its file; the mirror is named the way the
    join path names a joined org."""
    GraphDB.create_org_db("acme", type_="shared", org_id="local-acme-id").close()
    remote_uuid = "99999999-8888-7777-6666-555555555555"
    monkeypatch.setattr(follow_cmd, "_fetch_envelope",
                        lambda base, token, **kw: _envelope(remote_uuid, "acme"))
    written = _stop_at_the_row_write(monkeypatch)
    with pytest.raises(SystemExit):
        follow_cmd.cmd_follow_add(
            argparse.Namespace(target="https://relay.example/l/tok123#" + "b" * 64))
    assert written["key"] == "acme-99999999"
    assert written["payload"]["org_uuid"] == remote_uuid
    assert follow_cmd._local_org_id("acme") == "local-acme-id"


@pytest.mark.parametrize("published", ["personal", "machine"])
def test_a_published_reserved_name_never_names_a_local_store(orgs_env, monkeypatch, published):
    remote_uuid = "99999999-8888-7777-6666-555555555555"
    monkeypatch.setattr(follow_cmd, "_fetch_envelope",
                        lambda base, token, **kw: _envelope(remote_uuid, published))
    written = _stop_at_the_row_write(monkeypatch)
    with pytest.raises(SystemExit):
        follow_cmd.cmd_follow_add(
            argparse.Namespace(target="https://relay.example/l/tok123#" + "b" * 64))
    assert written["key"] == f"{published}-99999999"


def test_an_unreadable_store_is_never_taken(orgs_env):
    """A file at <slug>.db with no orgs row (partial, or busy mid-write) is
    someone's store: the uuid8 variant is chosen instead (review of step 2)."""
    from tools.graph import org_ops

    (orgs_env / "acme.db").write_bytes(b"")
    uuid = "99999999-8888-7777-6666-555555555555"
    assert org_ops.local_slug_for_org("acme", uuid) == "acme-99999999"
    (orgs_env / "acme-99999999.db").write_bytes(b"not a database")
    with pytest.raises(ValueError):
        org_ops.local_slug_for_org("acme", uuid)


@pytest.mark.parametrize("hint", ["None", "null", "undefined"])
def test_a_stringified_absence_hint_falls_through_to_the_uuid_variant(orgs_env, hint):
    from tools.graph import org_ops

    uuid = "99999999-8888-7777-6666-555555555555"
    assert org_ops.local_slug_for_org(hint, uuid) == f"{hint.lower()}-99999999"


def test_a_follow_after_a_join_lands_on_the_joined_file(orgs_env, monkeypatch):
    """Keyed by uuid: the org joined here as "acme-corp" is followed there,
    whatever slug the envelope names."""
    GraphDB.create_org_db("acme-corp", type_="shared", org_id="same-id").close()
    monkeypatch.setattr(follow_cmd, "_fetch_envelope",
                        lambda base, token, **kw: _envelope("same-id", "acme"))
    written = _stop_at_the_row_write(monkeypatch)
    with pytest.raises(SystemExit):
        follow_cmd.cmd_follow_add(
            argparse.Namespace(target="https://relay.example/l/tok123#" + "b" * 64))
    assert written["key"] == "acme-corp"


def test_an_explicit_as_that_collides_is_refused_naming_both_ids(orgs_env, monkeypatch, capsys):
    GraphDB.create_org_db("acme", type_="shared", org_id="local-acme-id").close()
    remote_uuid = "99999999-8888-7777-6666-555555555555"
    monkeypatch.setattr(follow_cmd, "_fetch_envelope",
                        lambda base, token, **kw: _envelope(remote_uuid, "whatever"))
    args = argparse.Namespace(target="https://relay.example/l/tok123#" + "b" * 64,
                              as_slug="acme")

    with pytest.raises(SystemExit) as exc:
        follow_cmd.cmd_follow_add(args)
    assert exc.value.code == 1

    err = capsys.readouterr().err
    assert "local-acme-id" in err
    assert remote_uuid in err
    assert "acme" in err


def test_follow_add_allows_matching_id_past_the_collision_guard(
    orgs_env, monkeypatch, capsys
):
    # A local org whose id MATCHES the followed org is not a collision (this is
    # the join-after-follow reconciliation case). The guard must not fire; the
    # command proceeds past it (later steps are exercised elsewhere, so we stop
    # it right after the guard by stubbing the row write).
    GraphDB.create_org_db("acme", type_="shared", org_id="same-id").close()
    monkeypatch.setattr(
        follow_cmd, "_fetch_envelope",
        lambda base, token, **kw: _envelope("same-id", "acme"),
    )

    calls = {}

    def _fake_add_setting(*a, **kw):
        calls["written"] = (a, kw)

    from tools.graph import settings_ops
    monkeypatch.setattr(settings_ops, "add_setting", _fake_add_setting)
    # Stop before the network import for the mirror.
    monkeypatch.setattr(
        "tools.network.fleet_sync_scheduler.materialize_follow_scopes",
        lambda: [],
    )
    args = argparse.Namespace(target="https://relay.example/l/tok123#" + "b" * 64)

    follow_cmd.cmd_follow_add(args)  # no SystemExit
    assert "written" in calls


def test_split_link_url_normalizes_the_base64url_fragment():
    """A published link carries its channel key as an unpadded base64url
    fragment (fragment_url); the follow row wants 64 hex. Both forms split
    to the same link_pub, and a wrong-length fragment is refused up front."""
    import base64

    raw = bytes(range(32))
    fragment = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    base, rendezvous, token, link_pub = follow_cmd._split_link_url(
        f"https://relay.example/l/tok123#{fragment}"
    )
    assert (base, rendezvous, token) == (
        "https://relay.example", "https://relay.example/l/tok123", "tok123",
    )
    assert link_pub == raw.hex()
    assert follow_cmd._split_link_url("https://relay.example/l/tok123#" + raw.hex())[3] == raw.hex()
    with pytest.raises(SystemExit):
        follow_cmd._split_link_url("https://relay.example/l/tok123#AAAA")


def test_follow_add_names_the_mirror_with_as_when_the_envelope_has_no_slug(
    orgs_env, monkeypatch, capsys
):
    """A link published before its org republished carries no meta.org.
    Without --as the command refuses and says so; with --as it proceeds past
    the slug resolution and the collision guard under that name."""
    remote_uuid = "99999999-8888-7777-6666-555555555555"
    monkeypatch.setattr(
        follow_cmd, "_fetch_envelope",
        lambda base, token, **kw: {"target_type": "org:follow",
                                   "org": remote_uuid, "meta": {}},
    )
    url = "https://relay.example/l/tok123#" + "b" * 64

    with pytest.raises(SystemExit):
        follow_cmd.cmd_follow_add(argparse.Namespace(target=url))
    assert "--as" in capsys.readouterr().err

    # Stop right after the guard by refusing the row write.
    written = {}

    def stop(set_id, rev, key, payload, org=None):
        written.update({"key": key, "payload": payload})
        raise RuntimeError("stop here")

    from tools.graph import settings_ops
    monkeypatch.setattr(settings_ops, "add_setting", stop)
    with pytest.raises(SystemExit):
        follow_cmd.cmd_follow_add(
            argparse.Namespace(target=url, as_slug="autonomy"))
    assert written["key"] == "autonomy"
    assert written["payload"]["org_uuid"] == remote_uuid
    assert written["payload"]["link_pub"] == "b" * 64

    with pytest.raises(SystemExit):
        follow_cmd.cmd_follow_add(
            argparse.Namespace(target=url, as_slug="Not A Slug"))
    assert "not a valid org slug" in capsys.readouterr().err
