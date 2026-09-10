"""Organization preparation must not require the founder's private org key.

The old unlock-plan gate required a non-replicated org-key row, excluding
joined members and second fleet machines from sign-in maintenance. A bound
organization with a founded ledger uses committed membership regardless of
whether this machine holds that row; delegate authorization remains the
ledger's responsibility.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.dashboard import membership_checkpoint, network_routes
from tools.graph import org_ops
from tools.graph.schemas.network_identity import NETWORK_BINDING_SET_ID, NETWORK_ORG_KEY_SET_ID

GENESIS = "ab" * 32
MEMBER_PERSONA = "cd" * 32


@pytest.fixture
def member_machine(monkeypatch, tmp_path):
    """Bound org; ledger whose fold lists our persona as a member; NO org-key row."""
    binding = SimpleNamespace(payload={
        "org_uuid": "8a2d6c7a-498c-42ba-a4a6-b3b27a024bac", "root_pub": "ef" * 32,
        "registry_url": "https://registry.invalid",
        "binding_expires_at": "2030-01-01T00:00:00Z", "recovery_policy": {"mode": "none"},
    })

    def first_member(set_id, org):
        if set_id == NETWORK_BINDING_SET_ID:
            return binding
        if set_id == NETWORK_ORG_KEY_SET_ID:
            return None            # invite-join never writes one
        return None

    monkeypatch.setattr(network_routes, "_first_member", first_member)
    ledger = tmp_path / "netorg.db"
    ledger.write_bytes(b"")

    class _Store:
        ledger = SimpleNamespace(genesis_id=GENESIS)

        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fold(self, now=None, heads=None):
            return SimpleNamespace(members={MEMBER_PERSONA: SimpleNamespace(roles=("member",))})

        def heads(self):
            return ["h1"]

    import tools.network.ledger as ledger_pkg
    monkeypatch.setattr(ledger_pkg, "org_ledger_db_path", lambda org: ledger)
    monkeypatch.setattr(ledger_pkg, "LedgerStore", _Store)
    monkeypatch.setattr(org_ops, "persona_pub_for_org", lambda genesis: MEMBER_PERSONA)
    from tools.dashboard import link_serving_supervisor
    monkeypatch.setattr(link_serving_supervisor, "serve_cert_requirement",
                        lambda org: {"required": True, "status": "expired", "days_remaining": 0})
    monkeypatch.setattr(membership_checkpoint, "checkpoint_status",
                        lambda org: {"needed": False, "checkpointer_pubs": []})
    return binding


def test_a_joined_members_machine_is_a_committed_membership_org(member_machine):
    entry = network_routes._org_unlock_plan("netorg", "netorg", ("personal", "machine"))
    assert entry["genesis_id"] == GENESIS
    assert entry["committed_membership_org"] is True, (
        "a member who joined by invitation holds no org-key row and is still a member"
    )


def test_organization_plans_prepares_the_members_org(member_machine, monkeypatch):
    from tools.dashboard import signon_preparation

    monkeypatch.setattr(org_ops, "list_orgs", lambda: [SimpleNamespace(slug="netorg", type="shared")])

    plans = list(signon_preparation.organization_plans())

    assert [entry["slug"] for entry, _persona in plans] == ["netorg"]
    assert plans[0][1] == MEMBER_PERSONA


@pytest.mark.parametrize("missing", ["org_uuid", "root_pub", "registry_url"])
def test_unregistered_org_is_not_prepared(member_machine, missing):
    member_machine.payload.pop(missing)
    entry = network_routes._org_unlock_plan("netorg", "netorg", ("personal", "machine"))
    assert entry["committed_membership_org"] is False


def test_local_store_is_not_prepared_even_with_binding_and_genesis(member_machine):
    entry = network_routes._org_unlock_plan("personal", "personal", ("personal", "machine"))
    assert entry["committed_membership_org"] is False


def test_absent_genesis_is_not_a_committed_org(member_machine, monkeypatch):
    import tools.network.ledger as ledger_pkg
    monkeypatch.setattr(ledger_pkg.LedgerStore, "ledger", SimpleNamespace(genesis_id=None))
    entry = network_routes._org_unlock_plan("netorg", "netorg", ("personal", "machine"))
    assert entry["committed_membership_org"] is False


def test_no_recorded_persona_does_not_prepare_a_delegate(member_machine, monkeypatch):
    from tools.dashboard import signon_preparation
    monkeypatch.setattr(org_ops, "list_orgs", lambda: [SimpleNamespace(slug="netorg")])
    monkeypatch.setattr(org_ops, "persona_pub_for_org", lambda genesis: None)
    assert list(signon_preparation.organization_plans()) == []
