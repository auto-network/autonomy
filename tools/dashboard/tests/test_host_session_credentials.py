"""A host terminal picks an account, the same way a container session does.

Its command used to set two environment variables and run ``claude``, with no
credential resolved at all — so the binary fell back to whatever sat in
``~/.claude``. That is one account, permanently. When it reaches its weekly
ceiling the host terminal cannot start, while other installed accounts sit
unused and the picker that would have chosen one is never consulted.
"""
from __future__ import annotations

import re

import pytest

from agents import session_launcher


HOST_CMD_TEMPLATE = (
    "CLAUDE_CODE_OAUTH_TOKEN={token} "
    "BD_ACTOR=terminal:{tmux} AUTONOMY_SESSION={tmux} "
    "claude --dangerously-skip-permissions --model {model}"
)


def _host_cmd(token: str, tmux: str = "host-0101-000000", model: str = "opus") -> str:
    """The command the handler builds, kept in one place for the assertions."""
    import shlex
    return HOST_CMD_TEMPLATE.format(
        token=shlex.quote(token), tmux=tmux, model=model)


def test_the_command_carries_the_resolved_account(monkeypatch):
    monkeypatch.setattr(
        session_launcher, "_resolve_credentials_via_substrate",
        lambda **kw: {"type": "token", "token": "tok-from-pool", "alias": "gmail"},
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    creds = session_launcher._resolve_credentials(prefer_alias=None)
    cmd = _host_cmd(creds["token"])

    assert "CLAUDE_CODE_OAUTH_TOKEN=tok-from-pool" in cmd
    assert cmd.index("CLAUDE_CODE_OAUTH_TOKEN") < cmd.index("claude ")


def test_an_alias_steers_the_choice(monkeypatch):
    seen = {}

    def picker(*, prefer_alias=None):
        seen["alias"] = prefer_alias
        return {"type": "token", "token": "tok", "alias": prefer_alias}

    monkeypatch.setattr(
        session_launcher, "_resolve_credentials_via_substrate", picker)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    session_launcher._resolve_credentials(prefer_alias="auto-network")

    assert seen["alias"] == "auto-network"


def test_a_token_needing_quoting_survives_the_shell():
    """The command is a shell string, so the value has to be quoted."""
    cmd = _host_cmd("tok with space;rm -rf /")

    assert "; rm" not in cmd and ";rm -rf /" not in cmd.split("BD_ACTOR")[0].replace(
        "'tok with space;rm -rf /'", "")
    assert re.search(r"CLAUDE_CODE_OAUTH_TOKEN='[^']*'", cmd)


def test_no_installed_account_is_a_refusal_not_a_silent_start(monkeypatch):
    """Starting a terminal that cannot authenticate wastes the operator's
    time twice: once waiting, once diagnosing."""
    monkeypatch.setattr(
        session_launcher, "_resolve_credentials_via_substrate",
        lambda **kw: None,
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    assert session_launcher._resolve_credentials(prefer_alias=None) is None


def test_an_operator_override_still_wins(monkeypatch):
    """The env var is a legacy operator override and keeps that meaning."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "operator-override")

    creds = session_launcher._resolve_credentials(prefer_alias="ignored")

    assert creds == {"type": "token", "token": "operator-override"}
