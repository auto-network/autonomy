"""auto-26e8a: organization-shared inference accounts, listed beside the
personal ones and marked by source; chosen explicitly (the auto-pick stays
personal); resolved from the organization's vault, also on a runner, where
it is the one secret the runner provides (graph://7eb29bc8-31a v6 D10)."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from agents import session_launcher as sl
from agents.tests.test_session_launcher import (  # noqa: F401 -- fixtures
    _run, captured_run, fake_crosstalk, neutralize_launch_preflight, platform_snapshot,
    signin_deliveries,
)
from tools.dashboard import harness_accounts
from tools.graph import harness_credentials as hv
from tools.graph.schemas.vault_credential import (
    ORG_HARNESS_ACCOUNTS_SET_ID, VAULT_AUDITED_SET_ID,
)

BUNDLE = {"access": "at-ORG", "refresh": "rt-ORG", "expires": "9000",
          "scopes": "user:inference", "alias": "team"}


def _row(key, value):
    return SimpleNamespace(key=key, id="r-" + key, payload={"value": value}, vault_error=None)


def _store(monkeypatch):
    """Personal rows in the audited set, acme's shared rows in its set."""
    rows = {
        (VAULT_AUDITED_SET_ID, None): [_row("claude.account.P1.alias", "mine"),
                                       _row("claude.account.P1.access", "at-P"),
                                       _row("claude.account.P1.refresh", "rt-P")],
        (ORG_HARNESS_ACCOUNTS_SET_ID, "acme"): [
            _row(f"claude.account.O1.{k}", v) for k, v in BUNDLE.items()],
    }
    reads = []

    def read_set(set_id, *, org, peers, key_prefix):
        reads.append((set_id, org))
        return SimpleNamespace(members=[r for r in rows.get((set_id, org), [])
                                        if r.key.startswith(key_prefix)])

    real = hv.list_accounts
    monkeypatch.setattr(hv, "list_accounts",
                        lambda h, *, read_set=None, org=None: real(h, read_set=read_set_, org=org))
    read_set_ = read_set
    monkeypatch.setattr(hv, "organization_slugs", lambda: ["acme"])
    return reads


def test_shared_accounts_are_listed_beside_personal_ones_with_their_source(monkeypatch):
    reads = _store(monkeypatch)
    accounts = hv.all_accounts("claude")
    assert [(a.id, a.source) for a in accounts] == [("P1", "personal"), ("O1", "acme")]
    assert hv.list_accounts("claude", org="acme")[0].launchable
    # A personal account is never read from, or listed under, an organization.
    assert [a.id for a in hv.list_accounts("claude", org="acme")] == ["O1"]
    assert (ORG_HARNESS_ACCOUNTS_SET_ID, "acme") in reads


def test_writing_a_shared_account_goes_to_the_organization_set(monkeypatch):
    from tools.graph import ops as graph_ops

    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setattr(hv, "read_account", lambda *a, **k: None)
    added = []
    monkeypatch.setattr(graph_ops, "add_setting",
                        lambda set_id, rev, key, payload, *, org, state: added.append(
                            (set_id, key, org)) or "id")
    hv.write_account("claude", "O2", {"alias": "shared"}, org="acme")
    assert added == [(ORG_HARNESS_ACCOUNTS_SET_ID, "claude.account.O2.alias", "acme")]


def test_the_chooser_lists_shared_accounts_and_never_recommends_one(monkeypatch):
    _store(monkeypatch)
    monkeypatch.setattr(harness_accounts, "_readings", lambda h: {})
    rows = {r["account_id"]: r for r in harness_accounts.account_rows("claude")}
    assert rows["O1"]["source"] == "acme" and not rows["O1"]["recommended"]
    assert rows["P1"]["source"] == "personal" and rows["P1"]["recommended"]
    assert "at-ORG" not in json.dumps(rows)
    assert harness_accounts.check_account("claude", "O1", "acme") is None
    assert harness_accounts.check_account("claude", "O1")[0] == "account-not-found"


# ── the runner path ────────────────────────────────────────────────────────


from tools.dashboard.tests.test_org_member_launch import (  # noqa: E402,F401
    CALLER, RUNNER, _body, _member_launch, _proj, server,
)


def test_a_member_may_name_their_organizations_shared_account_without_a_sign_in(
        server, monkeypatch):
    monkeypatch.setattr(harness_accounts, "check_account",
                        lambda h, a, org=None: None if (a, org) == ("O1", "alpha") else
                        ("account-not-found", "x"))
    body = _body(account="O1", account_org="alpha",
                 credentials={"credentials": {"github.token": "ghp_X"}})
    status, _data, seen = _member_launch(server, monkeypatch, body)
    assert status == 202
    assert (seen["body"]["account"], seen["body"]["account_org"]) == ("O1", "alpha")


@pytest.mark.parametrize("account, org", [("O1", "other-org"), (None, "alpha"), ("O1", None)])
def test_a_member_cannot_name_another_organizations_or_the_owners_account(
        server, monkeypatch, account, org):
    body = _body(account=account, account_org=org)
    status, data, seen = _member_launch(server, monkeypatch, body)
    assert (status, data["refusal"], seen) == (403, "account-not-launchable", {})


def test_home_carries_no_sign_in_for_a_shared_account_and_refuses_another_orgs(
        server, monkeypatch):
    monkeypatch.setattr(sl, "_resolve_credential", lambda key: "ghp_HOME")
    monkeypatch.setattr(sl, "_resolve_credentials_via_substrate",
                        lambda **_k: (_ for _ in ()).throw(AssertionError("own account")))
    carried = server._member_launch_credentials(_proj(), "claude", None, "O1", "alpha")
    assert carried["signins"] == {} and carried["env"] == {}
    assert carried["credentials"] == {"github.token": "ghp_HOME"}
    response = asyncio.run(server._launch_on_org_runner(
        {"machine": "e1e1e1e1", "project": "dev", "account": "O1", "account_org": "beta"},
        RUNNER))
    assert json.loads(response.body)["refusal"] == "account-not-launchable"
