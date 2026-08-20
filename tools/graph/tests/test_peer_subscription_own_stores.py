"""A peer subscription never removes the operator's own stores (auto-9uj7i).

Design of record graph://21a0da9e-1c2, driver D4. The subscription exists to
opt out of other ORGANIZATIONS; ``personal`` and ``machine`` never carry
another user's content and are always peers of an organization read — BOTH,
asserted individually, because an implementation built from a two-store
mental model re-adds personal and silently drops machine (which is where
schema metadata lives after auto-n77vh). The explicit ``peers`` kwarg stays
literal: ``peers=[]`` deliberately means own-store-only for identity,
credential and policy readers, and that meaning must survive.
"""

from __future__ import annotations

import pytest

from tools.graph import cross_org, settings_ops
from tools.graph import db as graph_db_mod
from tools.graph.db import GraphDB


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    for slug in ("autonomy", "anchore", "acme", "personal", "machine"):
        GraphDB.create_org_db(slug).close()
    yield root
    GraphDB.close_all_pooled()


def pin_subscription(caller: str, peers: list[str]) -> None:
    settings_ops.add_setting(
        cross_org.PEER_SUBSCRIPTION_SET_ID, 1,
        key=caller, payload={"peers": peers},
        org="personal", state="canonical",
    )


def test_a_pinned_subscription_naming_neither_still_resolves_both(orgs_root):
    pin_subscription("autonomy", ["anchore"])
    peers = cross_org.resolve_peers("autonomy", None)
    assert "anchore" in peers
    assert "acme" not in peers, "the pin still isolates from unnamed orgs"
    assert "personal" in peers
    assert "machine" in peers, (
        "machine must be asserted separately from personal — dropping it is "
        "the exact failure this rule exists to prevent"
    )


def test_an_empty_subscription_isolates_orgs_and_keeps_own_stores(orgs_root):
    pin_subscription("autonomy", [])
    peers = cross_org.resolve_peers("autonomy", None)
    assert "anchore" not in peers and "acme" not in peers
    assert "personal" in peers
    assert "machine" in peers


def test_an_absent_subscription_is_unchanged_every_peer(orgs_root):
    peers = cross_org.resolve_peers("autonomy", None)
    assert set(peers) == {"anchore", "acme", "personal", "machine"}


def test_explicit_empty_peers_still_means_own_store_only(orgs_root):
    """The deliberate isolation used by identity/credential/policy readers
    (read_owned_set and friends) is not weakened: no orgs, no personal, no
    machine."""
    assert cross_org.resolve_peers("autonomy", []) == []
    # And an explicit non-empty list stays literal too.
    assert cross_org.resolve_peers("autonomy", ["anchore"]) == ["anchore"]


def test_a_personal_answer_resolves_under_a_pinned_org_only_subscription(
    orgs_root, monkeypatch,
):
    """D4 end to end: the operator's personal published answer to an
    organizational question survives a subscription naming only other
    organizations."""
    monkeypatch.delenv("GRAPH_API", raising=False)
    from tools.graph.schemas.registry import (
        SCHEMAS,
        UPCONVERTERS,
        SettingSchema,
        register_schema,
    )

    schemas_snap, upcon_snap = dict(SCHEMAS), dict(UPCONVERTERS)
    try:
        class Sovereignty(SettingSchema):
            value: str

        register_schema("autonomy.test.sovereignty", 1, Sovereignty)
        pin_subscription("autonomy", ["anchore"])
        settings_ops.add_setting(
            "autonomy.test.sovereignty", 1, key="answer",
            payload={"value": "personal"}, org="personal", state="published",
        )
        members = settings_ops.read_set("autonomy.test.sovereignty", org="autonomy")
        by_key = {m.key: m for m in members.members}
        assert by_key["answer"].payload == {"value": "personal"}
    finally:
        SCHEMAS.clear(); SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear(); UPCONVERTERS.update(upcon_snap)


def test_own_stores_are_deduplicated_when_the_pin_names_them(orgs_root):
    pin_subscription("autonomy", ["personal", "machine", "anchore"])
    peers = cross_org.resolve_peers("autonomy", None)
    assert peers.count("personal") == 1
    assert peers.count("machine") == 1
