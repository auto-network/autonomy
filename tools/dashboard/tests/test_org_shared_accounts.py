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
from tools.graph.schemas.harness_account import (
    HARNESS_ACCOUNT_SET_ID, HARNESS_CREDENTIAL_SET_ID,
    ORG_HARNESS_ACCOUNT_SET_ID, ORG_HARNESS_CREDENTIAL_SET_ID,
)

BUNDLE = {"access": "at-ORG", "refresh": "rt-ORG", "expires": "9000",
          "scopes": "user:inference", "alias": "team"}


def _rows(account_id, parts):
    """The account's two rows (public, credential), built by the module's own mapping."""
    acct = hv.Account("claude", account_id, parts)
    acct.public.update({"harness": "claude", "account_id": account_id, "credential_state": "ok"})
    key = f"claude:{account_id}"
    return (SimpleNamespace(key=key, id="p-" + key, payload=acct.public, vault_error=None),
            SimpleNamespace(key=key, id="c-" + key, payload={"harness": "claude", **acct.secret},
                            vault_error=None))


def _store(monkeypatch):
    """Personal accounts in the personal sets, acme's shared ones in its sets."""
    p_public, p_cred = _rows("P1", {"alias": "mine", "access": "at-P", "refresh": "rt-P"})
    o_public, o_cred = _rows("O1", BUNDLE)
    rows = {
        (HARNESS_ACCOUNT_SET_ID, None): [p_public],
        (HARNESS_CREDENTIAL_SET_ID, None): [p_cred],
        (ORG_HARNESS_ACCOUNT_SET_ID, "acme"): [o_public],
        (ORG_HARNESS_CREDENTIAL_SET_ID, "acme"): [o_cred],
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
    assert (ORG_HARNESS_ACCOUNT_SET_ID, "acme") in reads
    assert (ORG_HARNESS_CREDENTIAL_SET_ID, "acme") in reads


def test_writing_a_shared_account_goes_to_the_organization_set(monkeypatch):
    from tools.graph import ops as graph_ops

    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setattr(hv, "read_account", lambda *a, **k: None)
    added = []
    monkeypatch.setattr(graph_ops, "write_by_key",
                        lambda set_id, rev, key, payload, *, org, state: added.append(
                            (set_id, key, org)) or "id")
    hv.write_account("claude", "O2", {"alias": "shared", "setup": "sk-ORG"}, org="acme")
    assert added == [(ORG_HARNESS_CREDENTIAL_SET_ID, "claude:O2", "acme"),
                     (ORG_HARNESS_ACCOUNT_SET_ID, "claude:O2", "acme")]


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


def test_credentials_import_with_org_seals_into_the_shared_set(monkeypatch, tmp_path):
    from tools.graph import credential_import as ci

    calls = []
    monkeypatch.setattr(ci, "import_claude", lambda home, **kw: calls.append(("claude", kw["org"]))
                        or ci.HarnessResult("claude", ci.STATUS_UNCHANGED, ""))
    monkeypatch.setattr(ci, "import_codex", lambda home, **kw: calls.append(("codex", kw["org"]))
                        or ci.HarnessResult("codex", ci.STATUS_UNCHANGED, ""))
    monkeypatch.setattr(ci, "import_grok", lambda home, **kw: calls.append(("grok", kw["org"]))
                        or ci.HarnessResult("grok", ci.STATUS_UNCHANGED, ""))
    ci.run_import(str(tmp_path), org="acme")
    assert calls == [("claude", "acme"), ("codex", "acme"), ("grok", "acme")]


def test_grok_import_reads_and_writes_the_organizations_set(monkeypatch, tmp_path):
    from tools.graph import credential_import as ci

    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "auth.json").write_text('{"user_id": "u1", "token": "t"}')
    seen = []
    monkeypatch.setattr(hv, "read_account", lambda h, a, **kw: seen.append(("read", kw.get("org"))))
    monkeypatch.setattr(hv, "write_account", lambda h, a, parts, **kw: seen.append(("write", kw.get("org"))))
    ci.import_grok(str(tmp_path), org="acme")
    assert seen == [("read", "acme"), ("write", "acme")]


def test_claude_list_and_remove_take_org(monkeypatch, capsys):
    from argparse import Namespace

    from tools.graph import claude_cmd

    _store(monkeypatch)
    claude_cmd.cmd_claude_list(Namespace(org="acme"))
    assert "team" in capsys.readouterr().out
    removed = []
    monkeypatch.setattr(hv, "remove_account", lambda h, a, org=None: removed.append((a, org)) or 1)
    claude_cmd.cmd_claude_remove(Namespace(alias="team", yes=True, org="acme"))
    assert removed == [("O1", "acme")]
