"""auto-dgr2c: the launcher records which account it chose and the reading
it chose on -- each window's used_percent and resets_at, and which accounts
it excluded as exhausted -- once per launch, in the session's
.session_meta.json and one log line, never with a credential. A Codex or
Grok account is picked once, so the record names the account whose sign-in
the session actually holds."""

from __future__ import annotations

import json

from agents import session_launcher
from agents.tests.test_session_launcher import (  # noqa: F401 -- fixtures
    _run,
    _stub_vault,
    captured_run,
    fake_crosstalk,
    neutralize_launch_preflight,
    platform_snapshot,
    signin_deliveries,
)
from tools.graph import harness_credentials as hv


def _claude_accounts(monkeypatch, *ids):
    _stub_vault(monkeypatch, {"claude": [
        hv.Account("claude", i, {"alias": f"alias-{i}", "access": "at-SECRET", "refresh": "rt",
                                 "expires": "9000", "scopes": "user:inference"})
        for i in ids]})


def _reading(account, short, resets=2_000_000_000):
    return {"harness": "claude", "account_id": account, "updated_at": "2026-10-02T00:00:00Z",
            "status": "ok", "windows": {"short": {"used_percent": short, "resets_at": resets},
                                        "long": {"used_percent": 10, "resets_at": resets}}}


def _readings(monkeypatch, rows, exhausted=()):
    monkeypatch.setattr(session_launcher, "_claude_usage_rows", lambda: rows)
    monkeypatch.setattr(session_launcher, "_reading_window_open", lambda p, now: True)
    monkeypatch.setattr(session_launcher, "_usage_exhausted",
                        lambda p, now: bool(p) and p["account_id"] in exhausted)


def test_an_exhausted_account_is_recorded_as_excluded_with_its_reading(monkeypatch):
    _claude_accounts(monkeypatch, "A", "B")
    _readings(monkeypatch, [_reading("A", 100), _reading("B", 40)], exhausted={"A"})
    sel = session_launcher._resolve_credentials_via_substrate(prefer_alias=None)["selection"]
    assert (sel["account_id"], sel["method"], sel["candidates"]) == ("B", "only", ["A", "B"])
    assert sel["excluded"] == [{"account_id": "A", "reason": "exhausted", "reading": {
        "updated_at": "2026-10-02T00:00:00Z", "status": "ok",
        "windows": {"short": {"used_percent": 100, "resets_at": 2_000_000_000},
                    "long": {"used_percent": 10, "resets_at": 2_000_000_000}}}}]
    assert sel["reading"]["windows"]["short"]["used_percent"] == 40


def test_every_account_exhausted_is_named_in_the_method(monkeypatch):
    _claude_accounts(monkeypatch, "A", "B")
    _readings(monkeypatch, [_reading("A", 100), _reading("B", 100)], exhausted={"A", "B"})
    sel = session_launcher._resolve_credentials_via_substrate(prefer_alias=None)["selection"]
    assert sel["method"].endswith("-all-exhausted") and sel["excluded"] == []


def test_an_alias_pick_still_records_the_reading(monkeypatch):
    _claude_accounts(monkeypatch, "A", "B")
    _readings(monkeypatch, [_reading("A", 70), _reading("B", 5)])
    sel = session_launcher._resolve_credentials_via_substrate(prefer_alias="alias-A")["selection"]
    assert (sel["account_id"], sel["method"]) == ("A", "alias")
    assert sel["reading"]["windows"]["short"]["used_percent"] == 70


def test_the_session_meta_holds_the_decision_and_no_secret(tmp_path, monkeypatch, fake_crosstalk,
                                                          captured_run):
    _claude_accounts(monkeypatch, "A")
    _readings(monkeypatch, [_reading("A", 12)])
    run = tmp_path / "run"
    _run(name="auto-sel", output_dir=str(run), harness="claude")
    meta = json.loads((run / "sessions" / ".session_meta.json").read_text())
    assert meta["harness_token"] == "A"
    assert meta["account_selection"]["account_id"] == "A"
    assert meta["account_selection"]["reading"]["windows"]["short"]["used_percent"] == 12
    assert "at-SECRET" not in json.dumps(meta)


def test_a_codex_session_records_the_account_it_was_given(tmp_path, monkeypatch, fake_crosstalk,
                                                         captured_run):
    parts = {"id": "i", "access": "a", "refresh": "r", "expires": "4102444800000"}
    _stub_vault(monkeypatch, {"codex": [hv.Account("codex", "C1", parts),
                                        hv.Account("codex", "C2", parts)]})
    # A second random pick would return the other account.
    picks = iter([1, 0, 1, 0])
    monkeypatch.setattr(session_launcher.random, "choice", lambda seq: seq[next(picks)])
    monkeypatch.setattr(session_launcher, "_codex_auth_doc", lambda acct: acct.id.encode())
    delivered = {}
    monkeypatch.setattr(session_launcher, "deliver_signins",
                        lambda name, payloads, **_k: delivered.update(payloads) or [])
    run = tmp_path / "run"
    _run(name="auto-cx", output_dir=str(run), harness="codex")
    meta = json.loads((run / "sessions" / ".session_meta.json").read_text())
    assert delivered[session_launcher.CODEX_AUTH_FILENAME].decode() == meta["harness_token"]
    assert meta["account_selection"] == {**meta["account_selection"], "harness": "codex",
                                         "account_id": meta["harness_token"],
                                         "method": "random", "candidates_count": 2}
