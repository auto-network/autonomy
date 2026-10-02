"""auto-raepo step 3: accounts are enumerated and chosen from their PUBLIC
rows without touching the vault, and a launch then opens exactly one
credential row -- the chosen account's. Run against a real warm vault (the
harness_credentials fixture), with the vault's open step instrumented."""

from __future__ import annotations

import random

import pytest

from agents import session_launcher as sl
from tools.graph import harness_credentials as hv
from tools.graph import settings_ops
from tools.graph.tests.test_harness_credentials import warm_vault  # noqa: F401 -- fixture

FAR = "9999999999999"


@pytest.fixture
def accounts(warm_vault, monkeypatch):  # noqa: F811
    hv.write_account("claude", "org-A", {"alias": "a", "access": "at-A", "refresh": "rt-A",
                                         "expires": FAR, "scopes": "user:inference"})
    hv.write_account("claude", "org-B", {"alias": "b", "setup": "sk-B",
                                         "setup_minted_at": "2026-09-01T00:00:00Z"})
    hv.write_account("codex", "cx-1", {"id": "idt", "access": "at-c", "refresh": "rt-c",
                                       "expires": FAR})
    hv.write_account("grok", "gk-1", {"auth": '{"t": 1}'})
    monkeypatch.setattr(sl, "_usage_rows", lambda harness: [])
    return warm_vault


def _forbid_vault(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the vault was read during enumeration or selection")
    monkeypatch.setattr(settings_ops, "_unwrap_vault_locator", forbidden)


def _count_opens(monkeypatch):
    opened = []
    real = settings_ops._unwrap_vault_locator

    def spy(locator, **kw):
        opened.append(kw["key"])
        return real(locator, **kw)
    monkeypatch.setattr(settings_ops, "_unwrap_vault_locator", spy)
    return opened


@pytest.mark.parametrize("harness, ids", [
    ("claude", {"org-A", "org-B"}), ("codex", {"cx-1"}), ("grok", {"gk-1"})])
def test_enumeration_and_selection_never_read_the_vault(accounts, monkeypatch, harness, ids):
    _forbid_vault(monkeypatch)
    assert {a.id for a in hv.list_public(harness)} == ids
    chosen, selection = sl.choose(harness, rng=random.Random(0))
    assert chosen.id in ids and selection["harness"] == harness
    assert sl.choose(harness, account_id=sorted(ids)[0])[0].id == sorted(ids)[0]


def test_the_chooser_lists_accounts_without_the_vault(accounts, monkeypatch):
    from tools.dashboard import harness_accounts
    _forbid_vault(monkeypatch)
    monkeypatch.setattr(harness_accounts, "_readings", lambda h: {})
    rows = harness_accounts.account_rows("claude")
    assert {r["account_id"] for r in rows} == {"org-A", "org-B"}
    assert all(r["launchable"] for r in rows)
    assert harness_accounts.check_account("claude", "org-A") is None


def test_a_claude_bundle_launch_opens_exactly_its_one_credential(accounts, monkeypatch):
    opened = _count_opens(monkeypatch)
    creds = sl._resolve_credentials_via_substrate(prefer_alias=None, account_id="org-A")
    assert creds["type"] == "vault" and opened == []         # chosen from public rows
    payloads = sl._signin_payloads("org-A", harness="claude")
    assert sl.CLAUDE_BUNDLE_FILENAME in payloads
    assert opened == ["claude:org-A"]


def test_a_claude_setup_token_launch_opens_exactly_its_one_credential(accounts, monkeypatch):
    opened = _count_opens(monkeypatch)
    creds = sl._resolve_credentials_via_substrate(prefer_alias=None, account_id="org-B")
    assert (creds["type"], creds["token"]) == ("token", "sk-B")
    assert opened == ["claude:org-B"]


@pytest.mark.parametrize("harness, key, filename", [
    ("codex", "codex:cx-1", sl.CODEX_AUTH_FILENAME),
    ("grok", "grok:gk-1", sl.GROK_AUTH_FILENAME)])
def test_a_codex_or_grok_launch_opens_exactly_its_one_credential(
        accounts, monkeypatch, harness, key, filename):
    opened = _count_opens(monkeypatch)
    payloads = sl._signin_payloads(None, harness=harness)
    assert filename in payloads
    assert opened == [key]


def test_a_redacted_credential_is_not_taken_for_a_token(monkeypatch):
    """Review of 25a4a16a2: through the settings routes a non-operator
    receives an organization credential as the redaction marker; it reads
    as not openable, never as a secret."""
    from types import SimpleNamespace

    from tools.dashboard import restricted_sets

    rows = {"autonomy.org.harness.account": SimpleNamespace(
                key="claude:O1", id="p", vault_error=None,
                payload={"harness": "claude", "account_id": "O1", "credential_state": "ok"}),
            "autonomy.org.vault.harness-credential": SimpleNamespace(
                key="claude:O1", id="c", vault_error=None,
                payload={"value": restricted_sets.REDACTED})}
    monkeypatch.setattr(hv, "_read_one", lambda set_id, key, org: rows[set_id])
    acct = hv.read_credential("claude", "O1", org="acme")
    assert acct.openable is False and acct.secret == {}


def _public(harness, account_id, **public):
    acct = hv.Account(harness, account_id, public={"harness": harness, "account_id": account_id,
                                                   **public})
    acct.opened = False
    return acct


def test_a_valid_setup_token_is_selectable_whatever_the_bundle_state():
    """Home 2026-10-02: both Claude accounts recorded refresh_failed
    (invalid_grant) beside setup tokens valid to 2027, and launched."""
    acct = _public("claude", "a", credential_state="refresh_failed",
                   setup_expires_at=9_999_999_999_999)
    assert sl._selectable("claude", acct)
    lapsed = _public("claude", "b", credential_state="refresh_failed", setup_expires_at=1)
    assert not sl._selectable("claude", lapsed)


def test_an_expired_codex_access_token_is_selectable():
    assert sl._selectable("codex", _public("codex", "c", credential_state="expired",
                                           credential_expires_at=1))
    assert not sl._selectable("codex", _public("codex", "d", credential_state="missing"))
    assert not sl._selectable("codex", _public("codex", "e", credential_state="refresh_failed"))
