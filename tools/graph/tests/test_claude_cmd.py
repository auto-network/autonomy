"""End-to-end tests for ``graph claude install`` / ``list`` / ``usage`` /
``remove`` (graph://73c4e9ef-bbc, bead auto-tyrly).

Mocks the OAuth helpers (``run_oauth_flow`` + ``mint_setup_token``) at the
module boundary so the assertions exercise the substrate writes, the
same-org sanity check, idempotency, and table rendering — no real
Anthropic round-trip required.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from tools.graph import cli, ops
from tools.graph.claude_oauth import FlowResult, OAuthError, TokenResponse
from tools.graph.schemas.claude_credentials import (
    CLAUDE_CREDENTIALS_REVISION,
    CLAUDE_CREDENTIALS_SET_ID,
)
from tools.graph.schemas.claude_setup_tokens import (
    CLAUDE_SETUP_TOKENS_REVISION,
    CLAUDE_SETUP_TOKENS_SET_ID,
)


# ── fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    # Agreement pin (the org-honest recipe for single-org modules, matching
    # test_credential_import.py): ``graph claude`` reads and writes at
    # explicit org='personal' like every other credential consumer, and a
    # pin at an arbitrary tmp graph.db contradicts that org under the
    # fail-loud resolver (OrgResolutionConflict). Point the pin AT the orgs
    # tree's own personal.db so pin and org resolution agree.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    db_path = orgs_dir.parent / "personal.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


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


def _make_token(
    *,
    org_uuid: str = "org-uuid-A",
    org_name: str = "Org A",
    account_email: str = "a@example.com",
    access_token: str = "at-A",
    refresh_token: str = "rt-A",
    expires_in: int = 3600,
    scope: str = "user:profile user:inference",
) -> TokenResponse:
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scope=scope,
        organization_uuid=org_uuid,
        organization_name=org_name,
        account_uuid="acct-A",
        account_email=account_email,
        raw={},
    )


def _make_flow(token: TokenResponse) -> FlowResult:
    return FlowResult(token=token, redirect_uri="http://localhost:5555/cb")


# ── install: full flow ───────────────────────────────────────


def test_install_writes_both_rows(graph_db_env):
    consumer_token = _make_token(scope="user:profile user:inference")
    console_token = _make_token(scope="org:create_api_key user:profile")
    flows = iter([_make_flow(consumer_token), _make_flow(console_token)])

    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-ant-oat01-XXXXX",
    ):
        rc, out, err = _run_cli([
            "claude", "install", "--alias", "gmail-max",
        ])

    assert rc == 0, err
    assert "Installed Claude account" in out

    creds = ops.read_set(CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG).members
    setups = ops.read_set(CLAUDE_SETUP_TOKENS_SET_ID, org=ops.CALLER_ORG).members
    assert len(creds) == 1
    assert len(setups) == 1
    assert creds[0].key == "org-uuid-A"
    assert setups[0].key == "org-uuid-A"
    assert creds[0].payload["alias"] == "gmail-max"
    assert creds[0].payload["access_token"] == "at-A"
    assert creds[0].payload["refresh_token"] == "rt-A"
    assert creds[0].payload["scopes"] == ["user:profile", "user:inference"]
    assert creds[0].payload["organization_name"] == "Org A"
    assert creds[0].payload["account_email"] == "a@example.com"
    assert setups[0].payload == {"raw_key": "sk-ant-oat01-XXXXX"}


def test_install_aborts_when_console_org_differs_from_consumer(graph_db_env):
    consumer_token = _make_token(org_uuid="org-uuid-A", account_email="a@x.com")
    console_token = _make_token(
        org_uuid="org-uuid-B", account_email="b@x.com",
    )
    flows = iter([_make_flow(consumer_token), _make_flow(console_token)])

    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-should-not-be-used",
    ) as mint:
        rc, _, err = _run_cli([
            "claude", "install", "--alias", "primary",
        ])

    assert rc != 0
    assert "different accounts" in err
    mint.assert_not_called()
    assert ops.read_set(
        CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG,
    ).members == []
    assert ops.read_set(
        CLAUDE_SETUP_TOKENS_SET_ID, org=ops.CALLER_ORG,
    ).members == []


def test_install_is_idempotent_for_same_alias_and_org(graph_db_env):
    """Re-running install with the same alias updates rows in place."""
    consumer_a = _make_token(access_token="at-A1", refresh_token="rt-A1")
    console_a = _make_token(access_token="at-A1c", refresh_token="rt-A1c")
    consumer_b = _make_token(access_token="at-A2", refresh_token="rt-A2")
    console_b = _make_token(access_token="at-A2c", refresh_token="rt-A2c")

    flows = iter([
        _make_flow(consumer_a), _make_flow(console_a),
        _make_flow(consumer_b), _make_flow(console_b),
    ])
    raw_keys = iter(["sk-ant-oat01-V1", "sk-ant-oat01-V2"])

    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        side_effect=lambda **_: next(raw_keys),
    ):
        rc, _, err = _run_cli(["claude", "install", "--alias", "gmail-max"])
        assert rc == 0, err
        rc, _, err = _run_cli(["claude", "install", "--alias", "gmail-max"])
        assert rc == 0, err

    creds = ops.read_set(CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG).members
    setups = ops.read_set(CLAUDE_SETUP_TOKENS_SET_ID, org=ops.CALLER_ORG).members
    assert len(creds) == 1
    assert len(setups) == 1
    assert creds[0].payload["access_token"] == "at-A2"
    assert creds[0].payload["refresh_token"] == "rt-A2"
    assert setups[0].payload == {"raw_key": "sk-ant-oat01-V2"}


def test_install_rejects_alias_collision_with_different_org(graph_db_env):
    """A second install with the same alias but a different Anthropic org
    must abort before any writes — the alias is already bound."""
    consumer_a = _make_token(org_uuid="org-uuid-A")
    console_a = _make_token(org_uuid="org-uuid-A")
    flows_a = iter([_make_flow(consumer_a), _make_flow(console_a)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows_a),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-1",
    ):
        rc, _, err = _run_cli(["claude", "install", "--alias", "primary"])
        assert rc == 0, err

    consumer_b = _make_token(org_uuid="org-uuid-B")
    flows_b = iter([_make_flow(consumer_b)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows_b),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-should-not-be-minted",
    ) as mint:
        rc, _, err = _run_cli(["claude", "install", "--alias", "primary"])

    assert rc != 0
    assert "different Anthropic org" in err
    mint.assert_not_called()
    creds = ops.read_set(CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG).members
    assert len(creds) == 1
    assert creds[0].key == "org-uuid-A"


def test_install_surfaces_oauth_error_cleanly(graph_db_env):
    def raise_first(**_):
        raise OAuthError("invalid_grant: user revoked")

    with patch(
        "tools.graph.claude_cmd.run_oauth_flow", side_effect=raise_first,
    ):
        rc, _, err = _run_cli(["claude", "install", "--alias", "primary"])
    assert rc != 0
    assert "invalid_grant" in err
    assert ops.read_set(
        CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG,
    ).members == []


# ── install --refresh-setup-token ────────────────────────────


def test_refresh_setup_token_replaces_only_setup_token_row(graph_db_env):
    consumer = _make_token()
    console_install = _make_token()
    console_refresh = _make_token()
    # First install drives 2 flows (consumer + console); refresh drives 1 more.
    flows = iter([
        _make_flow(consumer),
        _make_flow(console_install),
        _make_flow(console_refresh),
    ])
    raw_keys = iter(["sk-ant-oat01-V1", "sk-ant-oat01-V2"])

    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        side_effect=lambda **_: next(raw_keys),
    ):
        _run_cli(["claude", "install", "--alias", "primary"])
        creds_before = ops.read_set(
            CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG,
        ).members[0]

        rc, out, err = _run_cli([
            "claude", "install", "--alias", "primary",
            "--refresh-setup-token",
        ])

    assert rc == 0, err
    assert "Refreshed setup token" in out

    creds_after = ops.read_set(
        CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG,
    ).members[0]
    setups = ops.read_set(
        CLAUDE_SETUP_TOKENS_SET_ID, org=ops.CALLER_ORG,
    ).members
    # Credentials row was not rewritten — same id, same payload.
    assert creds_after.id == creds_before.id
    assert creds_after.payload == creds_before.payload
    # Setup-token row was replaced in place.
    assert len(setups) == 1
    assert setups[0].payload == {"raw_key": "sk-ant-oat01-V2"}


def test_refresh_setup_token_aborts_if_alias_unknown(graph_db_env):
    rc, _, err = _run_cli([
        "claude", "install", "--alias", "no-such-alias",
        "--refresh-setup-token",
    ])
    assert rc != 0
    assert "no-such-alias" in err


def test_refresh_setup_token_aborts_on_org_mismatch(graph_db_env):
    consumer = _make_token(org_uuid="org-uuid-A")
    console_a = _make_token(org_uuid="org-uuid-A")
    flows = iter([_make_flow(consumer), _make_flow(console_a)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-1",
    ):
        _run_cli(["claude", "install", "--alias", "primary"])

    different_console = _make_token(org_uuid="org-uuid-B")
    flows = iter([_make_flow(different_console)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-should-not-be-used",
    ) as mint:
        rc, _, err = _run_cli([
            "claude", "install", "--alias", "primary",
            "--refresh-setup-token",
        ])
    assert rc != 0
    assert "org-uuid-A" in err and "org-uuid-B" in err
    mint.assert_not_called()


# ── list ─────────────────────────────────────────────────────


def test_list_when_empty_prints_install_hint(graph_db_env):
    rc, out, _ = _run_cli(["claude", "list"])
    assert rc == 0
    assert "no Claude accounts installed" in out


def test_list_renders_installed_accounts(graph_db_env):
    consumer_a = _make_token(
        org_uuid="org-A", org_name="Org Alpha",
        account_email="a@x.com",
    )
    consumer_b = _make_token(
        org_uuid="org-B", org_name="Org Bravo",
        account_email="b@x.com",
    )
    console_a = _make_token(org_uuid="org-A", org_name="Org Alpha")
    console_b = _make_token(org_uuid="org-B", org_name="Org Bravo")

    flows = iter([
        _make_flow(consumer_a), _make_flow(console_a),
        _make_flow(consumer_b), _make_flow(console_b),
    ])
    raw_keys = iter(["sk-A", "sk-B"])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        side_effect=lambda **_: next(raw_keys),
    ):
        _run_cli(["claude", "install", "--alias", "alpha"])
        _run_cli(["claude", "install", "--alias", "bravo"])

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
    consumer = _make_token(org_uuid="org-X", org_name="Org X")
    console = _make_token(org_uuid="org-X", org_name="Org X")
    flows = iter([_make_flow(consumer), _make_flow(console)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-X",
    ):
        _run_cli(["claude", "install", "--alias", "primary"])

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


def test_remove_with_yes_drops_both_rows(graph_db_env):
    consumer = _make_token(org_uuid="org-X")
    console = _make_token(org_uuid="org-X")
    flows = iter([_make_flow(consumer), _make_flow(console)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-X",
    ):
        _run_cli(["claude", "install", "--alias", "primary"])

    rc, out, _ = _run_cli(["claude", "remove", "--alias", "primary", "--yes"])
    assert rc == 0
    assert "Removed Claude account" in out
    assert ops.read_set(
        CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG,
    ).members == []
    assert ops.read_set(
        CLAUDE_SETUP_TOKENS_SET_ID, org=ops.CALLER_ORG,
    ).members == []


def test_remove_unknown_alias_reports_and_exits_clean(graph_db_env):
    rc, out, _ = _run_cli(["claude", "remove", "--alias", "ghost", "--yes"])
    assert rc == 0
    assert "ghost" in out


def test_remove_aborts_on_no_confirmation(graph_db_env):
    consumer = _make_token(org_uuid="org-X")
    console = _make_token(org_uuid="org-X")
    flows = iter([_make_flow(consumer), _make_flow(console)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-X",
    ):
        _run_cli(["claude", "install", "--alias", "primary"])

    with patch("builtins.input", return_value="n"):
        rc, out, _ = _run_cli(["claude", "remove", "--alias", "primary"])
    assert rc == 0
    assert "Aborted" in out
    creds = ops.read_set(CLAUDE_CREDENTIALS_SET_ID, org=ops.CALLER_ORG).members
    assert len(creds) == 1


# ── secret-leak guard ────────────────────────────────────────


def test_install_does_not_print_bearer_credentials(graph_db_env):
    consumer = _make_token(
        access_token="at-SECRET-CONSUMER", refresh_token="rt-SECRET-CONSUMER",
    )
    console = _make_token(access_token="at-SECRET-CONSOLE")
    flows = iter([_make_flow(consumer), _make_flow(console)])
    with patch(
        "tools.graph.claude_cmd.run_oauth_flow",
        side_effect=lambda *, scope, **_: next(flows),
    ), patch(
        "tools.graph.claude_cmd.mint_setup_token",
        return_value="sk-ant-oat01-LEAKY",
    ):
        rc, out, err = _run_cli(["claude", "install", "--alias", "primary"])
    assert rc == 0
    combined = out + err
    # Bearer / refresh secrets must never reach stdout/stderr.
    assert "at-SECRET-CONSUMER" not in combined
    assert "rt-SECRET-CONSUMER" not in combined
    assert "at-SECRET-CONSOLE" not in combined
    assert "sk-ant-oat01-LEAKY" not in combined
