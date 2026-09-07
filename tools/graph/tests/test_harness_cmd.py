"""``graph harness`` — the account/usage diagnostic, read from any seat.

Bead auto-177b8. Two defects are pinned here.

The command used to default ``--org`` to ``personal`` and send it as
``X-Graph-Org``. The rows are ``@home("personal")``, but a container seat's
bearer names its own organization, and the dashboard refuses a request whose
bearer and header disagree — so every session saw "no harness accounts are
known" while the dashboard was serving the rows. Sending no org at all lets
the server resolve the declared home.

Live session counts came from ``/api/dao/recent_sessions``, which
deliberately excludes live sessions; the rows it does return include agentic
runs with no ``harness``, which is where ``--json``'s ``None:None`` key came
from.
"""
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

from tools.graph import harness_cmd


def _capture(args, *, responses) -> tuple[str, list[tuple[str, str | None]]]:
    """Run cmd_harness_status against canned API responses.

    Returns the printed output and every (path, org) the command asked for,
    so a test can assert on what went over the wire as well as what printed.
    """
    asked: list[tuple[str, str | None]] = []

    def _fake_get_json(path, *, org=None, timeout=30):
        asked.append((path, org))
        for prefix, payload in responses.items():
            if path.startswith(prefix):
                return payload
        raise AssertionError(f"unexpected request: {path}")

    original = harness_cmd._get_json
    harness_cmd._get_json = _fake_get_json
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            harness_cmd.cmd_harness_status(args)
        return out.getvalue(), asked
    finally:
        harness_cmd._get_json = original


def _usage_member(*, harness, alias=None, account_id=None, used=10.0):
    return {"payload": {
        "harness": harness, "alias": alias, "account_id": account_id,
        "identity_label": alias or "default", "status": "ok",
        "source": "probe_headers", "updated_at": "2026-09-07T21:00:00Z",
        "windows": {"long": {"used_percent": used, "window_minutes": 10080,
                             "resets_at": 4102444800}},
    }}


_TWO_CLAUDE_ACCOUNTS = {
    "/api/graph/settings/dashboard.harness.usage": {"members": [
        _usage_member(harness="claude", alias="gmail", account_id="org-G"),
        _usage_member(harness="claude", alias="auto-network", account_id="org-A"),
    ]},
    "/api/graph/settings/dashboard.claude.credentials": {"members": []},
    "/api/graph/settings/dashboard.codex.credentials": {"members": []},
    "/api/dao/session_status": {"rows": []},
}


def test_harness_status_sends_no_org_by_default():
    _, asked = _capture(
        argparse.Namespace(org=None, json=False), responses=_TWO_CLAUDE_ACCOUNTS,
    )
    settings_calls = [(p, o) for p, o in asked if "/settings/" in p]
    assert settings_calls, "expected at least one Settings read"
    assert all(org is None for _, org in settings_calls)


def test_harness_status_sends_an_org_only_when_asked():
    _, asked = _capture(
        argparse.Namespace(org="personal", json=False),
        responses=_TWO_CLAUDE_ACCOUNTS,
    )
    assert all(org == "personal" for p, org in asked if "/settings/" in p)


def test_live_counts_come_from_session_status_not_recent_sessions():
    _, asked = _capture(
        argparse.Namespace(org=None, json=False), responses=_TWO_CLAUDE_ACCOUNTS,
    )
    paths = [p for p, _ in asked]
    assert any("/api/dao/session_status" in p for p in paths)
    assert not any("recent_sessions" in p for p in paths)


def _with_sessions(rows):
    return {**_TWO_CLAUDE_ACCOUNTS, "/api/dao/session_status": {"rows": rows}}


def test_live_sessions_are_keyed_by_the_credential_they_launched_with():
    out, _ = _capture(argparse.Namespace(org=None, json=True), responses=_with_sessions([
        {"tmux_name": "host-1", "harness": "claude",
         "harness_token": "org-G", "state": "ACTIVE"},
        {"tmux_name": "host-2", "harness": "claude",
         "harness_token": "org-G", "state": "ACTIVE"},
        {"tmux_name": "host-3", "harness": "claude",
         "harness_token": "org-A", "state": "ACTIVE"},
    ]))
    live = json.loads(out)["live_sessions"]
    assert live == {"claude:gmail": 2, "claude:auto-network": 1}


def test_no_none_none_key_from_agentic_and_stub_rows():
    """The rows that produced ``None:None``: an agentic run and a
    half-populated stub, neither of which is an ACTIVE session."""
    out, _ = _capture(argparse.Namespace(org=None, json=True), responses=_with_sessions([
        {"title": "Refresh Preview", "harness": None, "harness_token": None},
        {"tmux_name": "old", "harness": "codex", "harness_token": None,
         "state": "ENDED", "activity_state": "dead"},
    ]))
    assert json.loads(out)["live_sessions"] == {}


def test_a_session_with_no_recorded_credential_is_reported_not_attributed():
    out, _ = _capture(argparse.Namespace(org=None, json=False), responses=_with_sessions([
        {"tmux_name": "auto-1", "harness": "claude",
         "harness_token": None, "state": "ACTIVE"},
        {"tmux_name": "auto-2", "harness": "claude",
         "harness_token": None, "state": "ACTIVE"},
        {"tmux_name": "host-1", "harness": "claude",
         "harness_token": "org-G", "state": "ACTIVE"},
    ]))
    assert "2 live claude session(s) are not attributed" in out
    # The attributable account still shows its own count, not the total.
    gmail_line = next(ln for ln in out.splitlines() if "gmail" in ln)
    assert gmail_line.split()[-1] == "1"


def test_json_names_an_unattributed_bucket_rather_than_null():
    out, _ = _capture(argparse.Namespace(org=None, json=True), responses=_with_sessions([
        {"tmux_name": "auto-1", "harness": "claude",
         "harness_token": None, "state": "ACTIVE"},
    ]))
    assert json.loads(out)["live_sessions"] == {"claude:unattributed": 1}


def test_refresh_error_prints_under_its_account():
    responses = {
        **_TWO_CLAUDE_ACCOUNTS,
        "/api/graph/settings/dashboard.claude.credentials": {"members": [
            {"payload": {"alias": "gmail",
                         "last_refresh_error": "invalid_grant: Refresh token not found"}},
        ]},
    }
    out, _ = _capture(argparse.Namespace(org=None, json=False), responses=responses)
    assert "refresh error: invalid_grant" in out


def test_dashboard_unreachable_reports_unknown_rather_than_zero():
    def _boom(path, *, org=None, timeout=30):
        raise OSError("connection refused")

    original = harness_cmd._get_json
    harness_cmd._get_json = _boom
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            harness_cmd.cmd_harness_status(argparse.Namespace(org=None, json=False))
        assert "no harness accounts are known" in out.getvalue()
    finally:
        harness_cmd._get_json = original
