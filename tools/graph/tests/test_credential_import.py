"""Tests for ``graph credentials import`` — Layer-0 credential import
(bead auto-5bq85).

Covers the bead's acceptance criteria directly:

* authed claude → substrate credentials row holds a working copy, and the
  user's original file is byte-identical afterwards;
* no auth present → ``needs_sign_in`` per harness, nothing imported,
  nothing prompted;
* idempotent → a second run is ``unchanged`` and never regresses a bundle
  the refresh poller already rotated;
* structural → the import path has no interactive secret prompt and makes
  no write outside the Settings store.

The Claude validation/identity call and the on-disk files are all
injectable / fixture-backed, so no real Anthropic round-trip is needed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from tools.graph import cli, ops
from tools.graph import credential_import as ci
from tools.graph.schemas.claude_credentials import (
    CLAUDE_CREDENTIALS_REVISION,
    CLAUDE_CREDENTIALS_SET_ID,
)
from tools.graph.schemas.codex_credentials import (
    CODEX_CREDENTIALS_SET_ID,
)


# ── fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _write_claude_file(home: Path, *, access="at-1", refresh="rt-1",
                       expires_at_ms=None, scopes=None,
                       subscription_type="max"):
    if expires_at_ms is None:
        expires_at_ms = int(time.time() * 1000) + 8 * 3600 * 1000
    bundle = {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_at_ms,
        "subscriptionType": subscription_type,
    }
    if scopes is not None:
        bundle["scopes"] = scopes
    else:
        bundle["scopes"] = ["user:profile", "user:inference"]
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": bundle}))
    return path


def _make_codex_jwt(*, email="dev@example.com", account_id="acct-9",
                    exp_epoch=None):
    if exp_epoch is None:
        exp_epoch = int(time.time()) + 3600
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = {"email": email, "account_id": account_id, "exp": exp_epoch}
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _write_codex_file(home: Path, *, auth_mode="chatgpt", access="ct-1",
                      refresh="cr-1", account_id="acct-9",
                      email="dev@example.com", exp_epoch=None,
                      api_key=None):
    tokens = {
        "id_token": _make_codex_jwt(
            email=email, account_id=account_id, exp_epoch=exp_epoch,
        ),
        "access_token": access,
        "refresh_token": refresh,
        "account_id": account_id,
    }
    doc = {
        "auth_mode": auth_mode,
        "OPENAI_API_KEY": api_key,
        "tokens": tokens,
        "last_refresh": "2026-08-10T04:59:00.925005Z",
    }
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


def _identity(org_uuid="org-A", org_name="Org A", email="dev@example.com"):
    def _fetch(_access_token):
        return ci.ClaudeIdentity(
            org_uuid=org_uuid, organization_name=org_name, account_email=email,
        )
    return _fetch


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved = sys.argv
    sys.argv = ["graph"] + argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved
    return rc, out.getvalue(), err.getvalue()


def _read_rows():
    members = ops.read_set(
        CLAUDE_CREDENTIALS_SET_ID, org=ci.CREDENTIALS_ORG, peers=[],
    )
    return {m.key: m.payload for m in members.members}


def _read_codex_rows():
    members = ops.read_set(
        CODEX_CREDENTIALS_SET_ID, org=ci.CREDENTIALS_ORG, peers=[],
    )
    return {m.key: m.payload for m in members.members}


# ── acceptance: authed claude imports a working copy ─────────


def test_authed_claude_imports_working_copy(graph_db_env, tmp_path):
    home = tmp_path / "home"
    src = _write_claude_file(home, access="at-live", refresh="rt-live")
    before = src.read_bytes()

    result = ci.import_claude(str(home), fetch_identity=_identity())

    assert result.status == ci.STATUS_IMPORTED
    rows = _read_rows()
    assert "org-A" in rows
    payload = rows["org-A"]
    assert payload["access_token"] == "at-live"
    assert payload["refresh_token"] == "rt-live"
    assert payload["account_email"] == "dev@example.com"
    assert payload["scopes"] == ["user:profile", "user:inference"]
    # bead: the user's original file is byte-identical afterwards
    assert src.read_bytes() == before


def test_default_alias_is_email_local_part(graph_db_env, tmp_path):
    home = tmp_path / "home"
    _write_claude_file(home)
    ci.import_claude(
        str(home), fetch_identity=_identity(email="jeremy@auto.network"),
    )
    assert _read_rows()["org-A"]["alias"] == "jeremy"


def test_alias_override(graph_db_env, tmp_path):
    home = tmp_path / "home"
    _write_claude_file(home)
    ci.import_claude(
        str(home), alias_override="gmail-max", fetch_identity=_identity(),
    )
    assert _read_rows()["org-A"]["alias"] == "gmail-max"


# ── acceptance: no auth → needs_sign_in, nothing written ─────


def test_no_auth_reports_needs_sign_in_and_writes_nothing(graph_db_env, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    report = ci.run_import(str(home))
    by_harness = {r.harness: r for r in report.results}
    assert by_harness["claude"].status == ci.STATUS_NEEDS_SIGN_IN
    assert by_harness["codex"].status == ci.STATUS_NEEDS_SIGN_IN
    assert _read_rows() == {}


def test_validation_failure_reports_needs_sign_in(graph_db_env, tmp_path):
    home = tmp_path / "home"
    _write_claude_file(home)

    def _fail(_tok):
        raise ci.CredentialImportError("profile endpoint returned HTTP 401")

    result = ci.import_claude(str(home), fetch_identity=_fail)
    assert result.status == ci.STATUS_NEEDS_SIGN_IN
    assert "401" in result.detail
    assert _read_rows() == {}


def test_keychain_backed_file_reports_needs_sign_in(graph_db_env, tmp_path):
    home = tmp_path / "home"
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True)
    # A keychain-backed login: JSON present but no claudeAiOauth bundle.
    path.write_text(json.dumps({"keychain": True}))
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_NEEDS_SIGN_IN
    assert "keychain" in result.detail
    assert _read_rows() == {}


# ── acceptance: idempotency + freshness ──────────────────────


def test_second_run_is_unchanged(graph_db_env, tmp_path):
    home = tmp_path / "home"
    exp = int(time.time() * 1000) + 8 * 3600 * 1000
    _write_claude_file(home, expires_at_ms=exp)
    first = ci.import_claude(str(home), fetch_identity=_identity())
    assert first.status == ci.STATUS_IMPORTED
    second = ci.import_claude(str(home), fetch_identity=_identity())
    assert second.status == ci.STATUS_UNCHANGED
    assert len(_read_rows()) == 1  # never duplicates


def test_reimport_does_not_regress_poller_rotated_bundle(graph_db_env, tmp_path):
    home = tmp_path / "home"
    old_exp = int(time.time() * 1000) + 3600 * 1000
    _write_claude_file(home, access="at-old", expires_at_ms=old_exp)
    ci.import_claude(str(home), fetch_identity=_identity())

    # Simulate the refresh poller rotating the row ahead of the on-disk file.
    ops.upsert_by_key(
        CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, "org-A",
        {
            "alias": "dev", "organization_name": "Org A",
            "account_email": "dev@example.com",
            "access_token": "at-rotated", "refresh_token": "rt-rotated",
            "expires_at_ms": old_exp + 8 * 3600 * 1000,
            "scopes": ["user:profile", "user:inference"],
            "last_refresh_at": "2026-08-13T00:00:00Z",
        },
        org=ci.CREDENTIALS_ORG,
    )
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_UNCHANGED
    # poller's fresher tokens survive; stale on-disk copy did not overwrite
    assert _read_rows()["org-A"]["access_token"] == "at-rotated"


def test_fresh_local_reauth_refreshes_row(graph_db_env, tmp_path):
    home = tmp_path / "home"
    old_exp = int(time.time() * 1000) + 3600 * 1000
    _write_claude_file(home, access="at-old", expires_at_ms=old_exp)
    ci.import_claude(str(home), fetch_identity=_identity())

    # User re-authed locally: a newer bundle on disk.
    _write_claude_file(
        home, access="at-new", refresh="rt-new",
        expires_at_ms=old_exp + 8 * 3600 * 1000,
    )
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_IMPORTED
    assert _read_rows()["org-A"]["access_token"] == "at-new"


def test_fresh_reauth_clears_stale_error_and_keeps_alias(graph_db_env, tmp_path):
    home = tmp_path / "home"
    old_exp = int(time.time() * 1000) + 3600 * 1000
    _write_claude_file(home, access="at-old", expires_at_ms=old_exp)
    ci.import_claude(
        str(home), alias_override="jeremy-auto", fetch_identity=_identity(),
    )
    # Poller marked the row revoked (its refresh_token had expired).
    existing = _read_rows()["org-A"]
    existing["last_refresh_error"] = "invalid_grant: Refresh token expired"
    ops.upsert_by_key(
        CLAUDE_CREDENTIALS_SET_ID, CLAUDE_CREDENTIALS_REVISION, "org-A",
        existing, org=ci.CREDENTIALS_ORG,
    )
    # User re-authed locally → a newer on-disk bundle triggers a clean write.
    _write_claude_file(
        home, access="at-new", refresh="rt-new",
        expires_at_ms=old_exp + 8 * 3600 * 1000,
    )
    ci.import_claude(str(home), fetch_identity=_identity())
    row = _read_rows()["org-A"]
    assert row["access_token"] == "at-new"
    assert row["alias"] == "jeremy-auto"                 # operator alias kept
    # Critical: stale revocation error cleared so the poller resumes refresh.
    assert "last_refresh_error" not in row


# ── codex ────────────────────────────────────────────────────


def test_codex_valid_imports_working_copy(graph_db_env, tmp_path):
    # STEP 1 (auto-kzws9): a validated ChatGPT-mode bundle is copied into
    # the substrate credential Setting, keyed by account_id.
    home = tmp_path / "home"
    src = _write_codex_file(
        home, email="dev@example.com", account_id="acct-9",
        access="ct-live", refresh="cr-live",
    )
    before = src.read_bytes()

    result = ci.import_codex(str(home))

    assert result.status == ci.STATUS_IMPORTED
    rows = _read_codex_rows()
    assert "acct-9" in rows
    payload = rows["acct-9"]
    assert payload["access_token"] == "ct-live"
    assert payload["refresh_token"] == "cr-live"
    assert payload["email"] == "dev@example.com"
    assert payload["auth_mode"] == "chatgpt"
    assert payload["id_token"]  # the JWT is stored
    assert isinstance(payload["expires_at_ms"], int)
    # bead: the user's original file is byte-identical afterwards
    assert src.read_bytes() == before


def test_codex_second_run_is_unchanged(graph_db_env, tmp_path):
    home = tmp_path / "home"
    _write_codex_file(home, account_id="acct-9")
    first = ci.import_codex(str(home))
    assert first.status == ci.STATUS_IMPORTED
    second = ci.import_codex(str(home))
    assert second.status == ci.STATUS_UNCHANGED
    assert len(_read_codex_rows()) == 1  # never duplicates


def test_codex_dry_run_writes_nothing(graph_db_env, tmp_path):
    home = tmp_path / "home"
    _write_codex_file(home, account_id="acct-9")
    result = ci.import_codex(str(home), dry_run=True)
    assert result.status == ci.STATUS_WOULD_IMPORT
    assert _read_codex_rows() == {}


def test_codex_expired_id_token_with_refresh_still_imports(graph_db_env, tmp_path):
    # Codex self-refreshes on launch, so an expired id_token on an account
    # that still has a refresh_token authenticates fine — and is still worth
    # importing (the successor poller will rotate it on the substrate row).
    home = tmp_path / "home"
    _write_codex_file(home, exp_epoch=int(time.time()) - 10, account_id="acct-9")
    result = ci.import_codex(str(home))
    assert result.status == ci.STATUS_IMPORTED
    assert _read_codex_rows()["acct-9"]["refresh_token"] == "cr-1"


def test_codex_expired_no_refresh_reports_needs_sign_in(tmp_path):
    home = tmp_path / "home"
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _make_codex_jwt(exp_epoch=int(time.time()) - 10),
            "access_token": "ct-1",
            "account_id": "acct-9",
        },
    }))
    result = ci.import_codex(str(home))
    assert result.status == ci.STATUS_NEEDS_SIGN_IN
    assert "expired" in result.detail


def test_codex_absent_reports_needs_sign_in(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = ci.import_codex(str(home))
    assert result.status == ci.STATUS_NEEDS_SIGN_IN


def test_codex_api_key_mode_is_valid(tmp_path):
    home = tmp_path / "home"
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-xyz", "tokens": {},
    }))
    result = ci.import_codex(str(home))
    assert result.status == ci.STATUS_IN_PLACE


# ── structural guarantees ────────────────────────────────────


def test_no_interactive_secret_prompt_in_module():
    """The import path must never prompt for a secret.

    AST-based so a mention of ``input()`` in a docstring doesn't count —
    only a real call to ``input`` / ``getpass`` fails the check.
    """
    import ast

    tree = ast.parse(Path(ci.__file__).read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert "input" not in called
    assert "getpass" not in called


def test_codex_api_key_path_makes_no_substrate_write(graph_db_env, tmp_path, monkeypatch):
    """API-key-mode Codex has no account-keyed row — it stays in place."""
    def _boom(*a, **k):
        raise AssertionError(
            "API-key-mode codex import must not write to the substrate"
        )

    monkeypatch.setattr(ops, "upsert_by_key", _boom)
    home = tmp_path / "home"
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "auth_mode": "apikey", "OPENAI_API_KEY": "sk-proj-xyz", "tokens": {},
    }))
    result = ci.import_codex(str(home))
    assert result.status == ci.STATUS_IN_PLACE


def test_dry_run_writes_nothing(graph_db_env, tmp_path):
    home = tmp_path / "home"
    _write_claude_file(home)
    result = ci.import_claude(
        str(home), dry_run=True, fetch_identity=_identity(),
    )
    assert result.status == ci.STATUS_WOULD_IMPORT
    assert _read_rows() == {}


# ── identity parsing ─────────────────────────────────────────


def test_parse_claude_identity_full():
    identity = ci.parse_claude_identity({
        "organization": {"uuid": "org-Z", "name": "Zeta"},
        "account": {"email_address": "z@zeta.io"},
    })
    assert identity.org_uuid == "org-Z"
    assert identity.organization_name == "Zeta"
    assert identity.account_email == "z@zeta.io"


def test_parse_claude_identity_missing_org_uuid_raises():
    with pytest.raises(ci.CredentialImportError):
        ci.parse_claude_identity({"account": {"email_address": "z@zeta.io"}})


def test_parse_claude_identity_name_falls_back_to_domain():
    identity = ci.parse_claude_identity({
        "organization": {"uuid": "org-Z"},
        "account": {"email_address": "z@zeta.io"},
    })
    assert identity.organization_name == "zeta.io"


# ── CLI wiring ───────────────────────────────────────────────


def test_cli_import_reports_both_harnesses(graph_db_env, tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_codex_file(home)  # codex authed, claude absent
    monkeypatch.setattr(
        ci, "_http_fetch_claude_identity", _identity(),
    )
    rc, out, err = _run_cli(["credentials", "import", "--home", str(home)])
    assert rc == 0
    assert "claude" in out
    assert "codex" in out
    assert ci.STATUS_NEEDS_SIGN_IN in out  # claude not authed
    assert ci.STATUS_IMPORTED in out       # codex bundle imported to substrate
