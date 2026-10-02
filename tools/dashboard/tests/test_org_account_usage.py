"""auto-elxua: usage for organization-shared accounts lives in the
organization (autonomy.org.harness-usage), written by whichever member's
machine takes a newer reading -- the Codex transcript where the session
runs, a one-token Claude probe when a member opens the chooser on a stale
reading -- and read back into the chooser's account list
(graph://7eb29bc8-31a v6 §11 Delta 6)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools.dashboard import harness_accounts
from tools.dashboard import harness_usage_settings as hus

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _reading(at, used=10, identity="O1", harness="claude"):
    return {"harness": harness, "identity_id": identity, "identity_label": "x", "status": "ok",
            "source": "probe_headers", "updated_at": _iso(at),
            "windows": {"short": {"used_percent": used, "resets_at": 1}}}


def _store(stored):
    writes = []

    def read_key(set_id, key, *, org, peers):
        assert set_id == hus.ORG_HARNESS_USAGE_SET_ID
        return {"payload": stored.get((org, key))} if (org, key) in stored else None

    def upsert(set_id, rev, key, payload, *, org, state):
        writes.append((set_id, org, key, payload["updated_at"]))
        stored[(org, key)] = payload

    return read_key, upsert, writes


def test_a_shared_reading_is_written_only_when_newer_than_the_stored_one():
    read_key, upsert, writes = _store({})
    assert hus.publish_org_reading("acme", "claude:O1", _reading(NOW), read_key=read_key,
                                   upsert_by_key=upsert)
    # Another member's machine holds an older reading: it does not roll back.
    assert not hus.publish_org_reading("acme", "claude:O1", _reading(NOW - timedelta(minutes=5)),
                                       read_key=read_key, upsert_by_key=upsert)
    assert hus.publish_org_reading("acme", "claude:O1", _reading(NOW + timedelta(minutes=1)),
                                   read_key=read_key, upsert_by_key=upsert)
    assert [w[0] for w in writes] == [hus.ORG_HARNESS_USAGE_SET_ID] * 2
    assert {w[1] for w in writes} == {"acme"}


def test_the_session_row_names_the_accounts_organization():
    assert hus.account_source({"account_selection": json.dumps({"source": "acme"})}) == "acme"
    assert hus.account_source({"account_selection": json.dumps({"source": "personal"})}) is None
    assert hus.account_source({"account_selection": None}) is None
    assert hus.account_source({"account_selection": "{not json"}) is None


def test_a_codex_session_on_a_shared_account_publishes_to_the_organization(monkeypatch):
    from tools.dashboard import session_monitor

    seen = []
    monkeypatch.setattr(hus, "publish_org_reading",
                        lambda org, key, payload, **_k: seen.append(("org", org, key)) or True)
    monkeypatch.setattr(hus, "publish_if_newer",
                        lambda key, payload, **_k: seen.append(("personal", key)) or True)
    state = {"kind": "rate_limits", "windows": {"long": {"used_percent": 3}},
             "updated_at": _iso(NOW)}
    session_monitor._publish_codex_harness_usage_setting(
        {"harness_token": "acct-9", "account_selection": json.dumps({"source": "acme"})}, state)
    session_monitor._publish_codex_harness_usage_setting(
        {"harness_token": "acct-1", "account_selection": None}, state)
    assert seen == [("org", "acme", "codex:acct-9"), ("personal", "codex:acct-1")]


def _rows(monkeypatch, shared_readings):
    from tools.graph import harness_credentials as hv

    accounts = [hv.Account("claude", "P1", {"alias": "mine", "access": "a", "refresh": "r"}),
                hv.Account("claude", "O1", {"alias": "team", "access": "a", "refresh": "r"},
                           source="acme")]
    monkeypatch.setattr(hv, "all_accounts", lambda h: accounts)
    monkeypatch.setattr(harness_accounts, "_readings", lambda h: {})
    monkeypatch.setattr(harness_accounts, "_org_readings",
                        lambda h, slugs: {"acme": shared_readings} if "acme" in slugs else {})
    return {r["account_id"]: r for r in harness_accounts.account_rows("claude")}


def test_a_shared_account_shows_its_organizations_reading(monkeypatch):
    rows = _rows(monkeypatch, {"O1": _reading(NOW, used=42)})
    assert rows["O1"]["usage"]["short"]["used_percent"] == 42
    assert rows["O1"]["usage"]["as_of"] == _iso(NOW)


def test_stale_or_missing_shared_readings_are_the_ones_refreshed(monkeypatch):
    fresh = _rows(monkeypatch, {"O1": _reading(NOW - timedelta(minutes=5))})
    assert harness_accounts.stale_shared_accounts(list(fresh.values()), now=NOW) == []
    old = _rows(monkeypatch, {"O1": _reading(NOW - timedelta(minutes=16))})
    assert [r["account_id"] for r in harness_accounts.stale_shared_accounts(
        list(old.values()), now=NOW)] == ["O1"]
    missing = _rows(monkeypatch, {})
    assert [r["account_id"] for r in harness_accounts.stale_shared_accounts(
        list(missing.values()), now=NOW)] == ["O1"]


def test_opening_the_chooser_probes_a_stale_shared_account_once_a_minute(monkeypatch):
    from tools.dashboard import server

    probed = []
    monkeypatch.setattr(server, "_org_claude_probe_started", {})
    monkeypatch.setattr(server, "_probe_org_claude_account",
                        lambda org, account: probed.append((org, account)))

    async def twice():
        first = server._schedule_org_claude_probe("acme", "O1")
        second = server._schedule_org_claude_probe("acme", "O1")
        await asyncio.sleep(0.05)
        return first, second

    assert asyncio.run(twice()) == (True, False)
    assert probed == [("acme", "O1")]


def test_a_probe_reads_the_shared_account_and_writes_the_organizations_set(monkeypatch):
    from tools.dashboard import server
    from tools.graph import harness_credentials as hv

    acct = hv.Account("claude", "O1", {"alias": "team", "setup": "sk-ORG"}, source="acme")
    monkeypatch.setattr(hv, "read_account",
                        lambda h, a, org=None: acct if (a, org) == ("O1", "acme") else None)
    monkeypatch.setattr(server, "_claude_usage_via_probe",
                        lambda token, **kw: _reading(NOW) if token == "sk-ORG" else None)
    written = []
    monkeypatch.setattr(hus, "publish_org_reading",
                        lambda org, key, payload, **_k: written.append((org, key)) or True)
    assert server._probe_org_claude_account("acme", "O1")
    assert written == [("acme", "claude:O1")]


def test_an_unchanged_shared_reading_is_refreshed_at_most_once_a_minute():
    read_key, upsert, writes = _store({})
    publish = lambda at, used: hus.publish_org_reading(  # noqa: E731
        "acme", "codex:acct-9", _reading(at, used=used, identity="acct-9", harness="codex"),
        read_key=read_key, upsert_by_key=upsert)
    assert publish(NOW, 10)
    assert not publish(NOW + timedelta(seconds=20), 10)      # same usage, 20 s later
    assert publish(NOW + timedelta(seconds=30), 11)          # usage moved
    assert publish(NOW + timedelta(seconds=95), 11)          # same usage, over a minute
    assert len(writes) == 3
