"""Tests for ``graph credentials import`` — the harness sign-ins found on
this machine, sealed into the operator's vault (bead auto-5bq85, then
graph://5f2f5a49-00d v12 FR7a and the vault rubric).

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
from tools.graph import harness_credentials as hv
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.vault import key_holder
from tools.vault.personal_object import derive_delegate_audited_recipient
from tools.vault.store import VaultStore


# ── fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    # Agreement pin (the org-honest recipe for single-org modules):
    # credential consumers write at explicit org='personal', and a pin
    # at an arbitrary tmp graph.db contradicts that org under the
    # fail-loud resolver (OrgResolutionConflict). Point the pin AT the
    # orgs tree's own personal.db so pin and org resolution agree.
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    db_path = orgs_dir.parent / "personal.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
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



# ── the vault, warm ──────────────────────────────────────────


@pytest.fixture
def warm_vault(tmp_path, monkeypatch):
    """A personal store whose audited vault can seal (delegate recipient
    published, as first run and identity creation do) and open (delegate key
    warm, as unlock does)."""
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


def _sealed(*keys):
    return hv.open_values(keys)


# ── acceptance: authed claude is sealed into the vault ───────


def test_authed_claude_is_sealed_and_the_file_untouched(warm_vault, tmp_path):
    home = tmp_path / "home"
    src = _write_claude_file(home, access="at-live", refresh="rt-live")
    before = src.read_bytes()
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_IMPORTED
    got = _sealed(*hv.CLAUDE_KEYS)
    assert got[hv.CLAUDE_ACCESS] == "at-live"
    assert got[hv.CLAUDE_REFRESH] == "rt-live"
    assert got[hv.CLAUDE_ACCOUNT] == "dev@example.com"
    assert got[hv.CLAUDE_SCOPES] == "user:profile,user:inference"
    assert hv.expires_ms(got[hv.CLAUDE_EXPIRES]) > 0
    assert src.read_bytes() == before


def test_sealed_rows_hold_no_plaintext(warm_vault, tmp_path):
    """What the database file shows is the row's name and ciphertext."""
    home = tmp_path / "home"
    _write_claude_file(home, access="at-secret-value")
    ci.import_claude(str(home), fetch_identity=_identity())
    import sqlite3
    conn = sqlite3.connect(str(warm_vault))
    try:
        blobs = " ".join(
            str(r[0]) for r in conn.execute("SELECT payload FROM settings")
        )
    finally:
        conn.close()
    assert "at-secret-value" not in blobs


def test_no_auth_reports_needs_sign_in_and_seals_nothing(warm_vault, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    report = ci.run_import(str(home))
    by_harness = {r.harness: r for r in report.results}
    assert by_harness["claude"].status == ci.STATUS_NEEDS_SIGN_IN
    assert by_harness["codex"].status == ci.STATUS_NEEDS_SIGN_IN
    assert by_harness["grok"].status == ci.STATUS_NEEDS_SIGN_IN
    assert hv.rows() == {}


def test_validation_failure_reports_needs_sign_in(warm_vault, tmp_path):
    home = tmp_path / "home"
    _write_claude_file(home)

    def _fail(_tok):
        raise ci.CredentialImportError("profile endpoint returned HTTP 401")

    result = ci.import_claude(str(home), fetch_identity=_fail)
    assert result.status == ci.STATUS_NEEDS_SIGN_IN
    assert "401" in result.detail
    assert hv.rows() == {}


def test_keychain_backed_file_reports_needs_sign_in(warm_vault, tmp_path):
    home = tmp_path / "home"
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"keychain": True}))
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_NEEDS_SIGN_IN
    assert "keychain" in result.detail
    assert hv.rows() == {}


# ── acceptance: idempotency + freshness ──────────────────────


def test_second_run_is_unchanged(warm_vault, tmp_path):
    home = tmp_path / "home"
    exp = int(time.time() * 1000) + 8 * 3600 * 1000
    _write_claude_file(home, expires_at_ms=exp)
    assert ci.import_claude(str(home), fetch_identity=_identity()).status == ci.STATUS_IMPORTED
    assert ci.import_claude(str(home), fetch_identity=_identity()).status == ci.STATUS_UNCHANGED
    assert set(hv.rows()) == set(hv.CLAUDE_KEYS)


def test_reimport_does_not_regress_a_rotated_sign_in(warm_vault, tmp_path):
    home = tmp_path / "home"
    old_exp = int(time.time() * 1000) + 3600 * 1000
    _write_claude_file(home, access="at-old", expires_at_ms=old_exp)
    ci.import_claude(str(home), fetch_identity=_identity())
    # The refresh poller rotated the sealed sign-in ahead of the on-disk file.
    hv.seal(hv.CLAUDE_ACCESS, "at-rotated")
    hv.seal(hv.CLAUDE_EXPIRES, str(old_exp + 8 * 3600 * 1000))
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_UNCHANGED
    assert _sealed(hv.CLAUDE_ACCESS)[hv.CLAUDE_ACCESS] == "at-rotated"


def test_fresh_local_reauth_reseals(warm_vault, tmp_path):
    home = tmp_path / "home"
    old_exp = int(time.time() * 1000) + 3600 * 1000
    _write_claude_file(home, access="at-old", expires_at_ms=old_exp)
    ci.import_claude(str(home), fetch_identity=_identity())
    _write_claude_file(
        home, access="at-new", refresh="rt-new",
        expires_at_ms=old_exp + 8 * 3600 * 1000,
    )
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_IMPORTED
    assert _sealed(hv.CLAUDE_ACCESS)[hv.CLAUDE_ACCESS] == "at-new"


def test_cold_vault_seals_as_found(warm_vault, tmp_path):
    """Sealing needs no unlock; the freshness comparison does, and without it
    the on-disk sign-in is sealed as found."""
    settings_ops.set_personal_delegate_audited_key(None)
    home = tmp_path / "home"
    _write_claude_file(home, access="at-cold")
    result = ci.import_claude(str(home), fetch_identity=_identity())
    assert result.status == ci.STATUS_IMPORTED
    assert hv.present(hv.CLAUDE_REQUIRED)
    assert hv.open_value(hv.CLAUDE_ACCESS) is None  # cold: present, not openable


def test_dry_run_seals_nothing(warm_vault, tmp_path):
    home = tmp_path / "home"
    _write_claude_file(home)
    result = ci.import_claude(str(home), dry_run=True, fetch_identity=_identity())
    assert result.status == ci.STATUS_WOULD_IMPORT
    assert hv.rows() == {}


# ── codex ────────────────────────────────────────────────────


def test_codex_valid_is_sealed(warm_vault, tmp_path):
    home = tmp_path / "home"
    src = _write_codex_file(
        home, email="dev@example.com", account_id="acct-9",
        access="ct-live", refresh="cr-live",
    )
    before = src.read_bytes()
    result = ci.import_codex(str(home))
    assert result.status == ci.STATUS_IMPORTED
    got = _sealed(*hv.CODEX_KEYS)
    assert got[hv.CODEX_ACCESS] == "ct-live"
    assert got[hv.CODEX_REFRESH] == "cr-live"
    assert got[hv.CODEX_ACCOUNT] == "acct-9"
    assert got[hv.CODEX_ID]
    assert hv.expires_ms(got[hv.CODEX_EXPIRES]) > 0
    assert src.read_bytes() == before


def test_codex_second_run_is_unchanged(warm_vault, tmp_path):
    home = tmp_path / "home"
    _write_codex_file(home)
    assert ci.import_codex(str(home)).status == ci.STATUS_IMPORTED
    assert ci.import_codex(str(home)).status == ci.STATUS_UNCHANGED


def test_codex_dry_run_seals_nothing(warm_vault, tmp_path):
    home = tmp_path / "home"
    _write_codex_file(home)
    assert ci.import_codex(str(home), dry_run=True).status == ci.STATUS_WOULD_IMPORT
    assert hv.rows() == {}


def test_codex_expired_id_token_with_refresh_still_seals(warm_vault, tmp_path):
    home = tmp_path / "home"
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _make_codex_jwt(exp_epoch=int(time.time()) - 10),
            "access_token": "ct-1", "refresh_token": "cr-1", "account_id": "acct-9",
        },
    }))
    result = ci.import_codex(str(home))
    assert result.status == ci.STATUS_IMPORTED
    assert _sealed(hv.CODEX_REFRESH)[hv.CODEX_REFRESH] == "cr-1"


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


def test_parse_claude_identity_full():
    identity = ci.parse_claude_identity({
        "organization": {"uuid": "org-Z", "name": "Zeta"},
        "account": {"email": "z@zeta.io"},
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


def test_operator_home_prefers_the_host_home(monkeypatch):
    monkeypatch.setenv("AUTONOMY_HOST_HOME", "/hosthome/op")
    assert ci.operator_home() == "/hosthome/op"
    monkeypatch.delenv("AUTONOMY_HOST_HOME")


# ── grok ─────────────────────────────────────────────────────


def test_grok_absent_reports_needs_sign_in(tmp_path):
    assert ci.import_grok(str(tmp_path / "home")).status == ci.STATUS_NEEDS_SIGN_IN


def test_grok_stored_sign_in_is_sealed_verbatim(warm_vault, tmp_path):
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    src = home / ".grok" / "auth.json"
    src.write_text(json.dumps({"access_token": "g-1", "user": "dev"}))
    before = src.read_bytes()
    assert ci.import_grok(str(home)).status == ci.STATUS_IMPORTED
    assert hv.open_value(hv.GROK_AUTH) == before.decode()
    assert src.read_bytes() == before
    assert ci.import_grok(str(home)).status == ci.STATUS_UNCHANGED


def test_run_import_reports_three_harnesses_and_usable_list(warm_vault, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    data = ci.report_to_dict(ci.run_import(str(home), dry_run=True))
    assert [r["harness"] for r in data["harnesses"]] == ["claude", "codex", "grok"]
    assert data["usable"] == []
    (home / ".grok").mkdir()
    (home / ".grok" / "auth.json").write_text(json.dumps({"access_token": "g-1"}))
    data = ci.report_to_dict(ci.run_import(str(home), dry_run=True))
    assert data["usable"] == ["grok"]


def test_cli_import_reports_three_harnesses(warm_vault, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rc, out, err = _run_cli(["credentials", "import", "--home", str(home)])
    assert rc == 0, err
    for harness in ("claude", "codex", "grok"):
        assert harness in out
    assert "needs_sign_in" in out
