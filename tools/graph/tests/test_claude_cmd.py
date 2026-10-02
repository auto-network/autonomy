"""End-to-end tests for ``graph claude install`` / ``list`` / ``usage`` /
``remove`` (graph://73c4e9ef-bbc, bead auto-tyrly).

Accounts are seeded the way an import writes them (``graph claude install``
signs nothing in since auto-n9tdh), so the assertions exercise table
rendering, usage and removal -- no Anthropic round-trip.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from tools.graph import cli, ops
from tools.graph import harness_credentials as hv
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.vault import key_holder
from tools.vault.personal_object import derive_delegate_audited_recipient
from tools.vault.store import VaultStore


# ── fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """A personal store whose audited vault seals and opens: the accounts the
    command writes are vault rows (record v16 §10.9)."""
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    db = tmp_path / "personal.db"
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: db)
    GraphDB(db).close()
    GraphDB.close_all_pooled()
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(public_hex)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(private_hex)
    yield db
    GraphDB.close_all_pooled()
    settings_ops.set_personal_delegate_audited_key(None)


class _View:
    """An account seen the way the old rows were asserted on."""
    def __init__(self, acct, payload):
        self.id = acct.id
        self.key = acct.id
        self.payload = payload


def _creds():
    out = []
    for acct in hv.list_accounts("claude"):
        if not acct.has("refresh"):
            continue
        out.append(_View(acct, {
            "alias": acct.get("alias"), "organization_name": acct.get("org_name"),
            "account_email": acct.get("email"), "access_token": acct.get("access"),
            "refresh_token": acct.get("refresh"), "scopes": hv.scopes_list(acct.get("scopes")),
            "expires_at_ms": acct.expires_ms(),
        }))
    return out


def _setups():
    return [
        _View(acct, {"raw_key": acct.get("setup")})
        for acct in hv.list_accounts("claude") if acct.get("setup")
    ]


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    sys.argv = ["graph"] + argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved_argv
    return rc, out.getvalue(), err.getvalue()


def _seed(org_uuid, alias, *, org_name=None, email=None, setup=None):
    """An installed account, written the way an import writes one."""
    parts = {"alias": alias, "access": "at-" + org_uuid, "refresh": "rt-" + org_uuid,
             "expires": "9999999999999", "scopes": "user:inference"}
    if org_name:
        parts["org_name"] = org_name
    if email:
        parts["email"] = email
    if setup:
        parts.update({"setup": setup, "setup_minted_at": "2026-09-01T00:00:00Z"})
    hv.write_account("claude", org_uuid, parts)


# ── install: full flow ───────────────────────────────────────


# ── install --refresh-setup-token ────────────────────────────


# ── list ─────────────────────────────────────────────────────


def test_list_when_empty_prints_install_hint(graph_db_env):
    rc, out, _ = _run_cli(["claude", "list"])
    assert rc == 0
    assert "no Claude accounts installed" in out


def test_list_renders_installed_accounts(graph_db_env):
    _seed("org-A", "alpha", org_name="Org Alpha", email="a@x.com", setup="sk-A")
    _seed("org-B", "bravo", org_name="Org Bravo", email="b@x.com", setup="sk-B")

    rc, out, _ = _run_cli(["claude", "list"])
    assert rc == 0
    assert "alpha" in out and "bravo" in out
    assert "Org Alpha" in out and "Org Bravo" in out
    assert "a@x.com" in out and "b@x.com" in out
    # Bearer credentials must not leak into the rendered table.
    assert "sk-A" not in out and "sk-B" not in out
    assert "at-A" not in out


# ── usage ────────────────────────────────────────────────────


def test_usage_renders_alias_and_window_pcts(graph_db_env):
    _seed("org-X", "primary", org_name="Org X", setup="sk-X")

    # Seed a harness-usage row keyed to the same Anthropic org. The
    # ``dashboard.harness.usage`` schema is registered separately by the
    # dashboard package; import it here so substrate validation accepts
    # the write.
    import tools.dashboard.harness_usage_settings  # noqa: F401 — registers schema

    ops.upsert_by_key(
        "dashboard.harness.usage",
        1,
        "claude:org:org-X",
        {
            "harness": "claude",
            "identity_id": "org:org-X",
            "identity_label": "org org-X",
            "alias": None,
            "account_id": "org-X",
            "status": "ok",
            "source": "oauth_usage",
            "updated_at": "2026-05-06T01:23:45Z",
            "windows": {
                "short": {
                    "used_percent": 12.0,
                    "window_minutes": 300,
                    "resets_at": 1_900_000_000,
                },
                "long": {
                    "used_percent": 7.0,
                    "window_minutes": 10080,
                    "resets_at": 1_900_500_000,
                },
            },
        },
        org=ops.CALLER_ORG,
    )

    rc, out, _ = _run_cli(["claude", "usage"])
    assert rc == 0
    assert "primary" in out
    assert "12%" in out
    assert "7%" in out


def test_usage_when_no_accounts(graph_db_env):
    rc, out, _ = _run_cli(["claude", "usage"])
    assert rc == 0
    assert "no Claude accounts installed" in out


# ── remove ───────────────────────────────────────────────────


def test_remove_with_yes_drops_the_account(graph_db_env):
    _seed("org-X", "primary", setup="sk-X")

    rc, out, _ = _run_cli(["claude", "remove", "--alias", "primary", "--yes"])
    assert rc == 0
    assert "Removed Claude account" in out
    assert _creds() == []
    assert _setups() == []


def test_remove_unknown_alias_reports_and_exits_clean(graph_db_env):
    rc, out, _ = _run_cli(["claude", "remove", "--alias", "ghost", "--yes"])
    assert rc == 0
    assert "ghost" in out


def test_remove_aborts_on_no_confirmation(graph_db_env):
    _seed("org-X", "primary", setup="sk-X")

    with patch("builtins.input", return_value="n"):
        rc, out, _ = _run_cli(["claude", "remove", "--alias", "primary"])
    assert rc == 0
    assert "Aborted" in out
    creds = _creds()
    assert len(creds) == 1


# ── secret-leak guard ────────────────────────────────────────


def test_install_prints_the_two_ways_an_account_gets_in(graph_db_env):
    """auto-n9tdh: the hand-rolled OAuth install is gone; install names the
    two routes that work and writes nothing."""
    rc, out, _ = _run_cli(["claude", "install"])
    assert rc == 0
    assert "graph credentials import" in out and "claude setup-token" in out
    assert "graph://5ab13dd5-570" in out
    assert _creds() == [] and _setups() == []


# ── auto-177b8: reading the rows from a container seat ────────


def test_read_rows_uses_the_dashboard_when_graph_api_is_set(monkeypatch):
    """In a container the rows live on the host, so the local personal.db is
    empty and a direct read returns nothing. Route through the dashboard,
    and send NO X-Graph-Org: the server resolves the row's declared home,
    while naming ``personal`` from a session bearer is refused outright."""
    from types import SimpleNamespace
    from tools.graph import claude_cmd

    monkeypatch.setenv("GRAPH_API", "https://localhost:8080")
    monkeypatch.setattr(
        claude_cmd.ops, "read_set",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not read the local DB in container mode")),
    )
    seen: list[dict] = []

    class _Client:
        def read_set(self, set_id, *, org, peers=None):
            seen.append({"set_id": set_id, "org": org})
            return SimpleNamespace(members=["row"])

    monkeypatch.setattr("tools.graph.client.get_client", lambda: _Client())

    assert claude_cmd._read_rows("dashboard.harness.usage") == ["row"]
    assert seen == [{"set_id": "dashboard.harness.usage", "org": None}]


def test_read_rows_reads_the_local_db_at_its_home_without_graph_api(monkeypatch):
    """On the host there is no dashboard indirection, and the home has to be
    named: the ambient GRAPH_ORG would read a different database than the
    running system writes."""
    from types import SimpleNamespace
    from tools.graph import claude_cmd

    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("GRAPH_ORG", "autonomy")
    monkeypatch.setattr(
        "tools.graph.client.get_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not use HTTP on the host")),
    )
    seen: list[dict] = []

    def _read_set(set_id, *, org, peers=None):
        seen.append({"set_id": set_id, "org": org})
        return SimpleNamespace(members=["row"])

    monkeypatch.setattr(claude_cmd.ops, "read_set", _read_set)

    assert claude_cmd._read_rows("dashboard.claude.credentials") == ["row"]
    assert seen == [{"set_id": "dashboard.claude.credentials", "org": "personal"}]

