"""Renaming a session is the session naming itself, not a cross-org edit.

`api_session_label` retitled the session's graph source under whichever
organization the CALLER was scoped to. When the source lived in another one --
routine, since a session is ingested under the org it works for -- the write
raised CrossOrgWriteError. Unhandled, so Starlette returned its plain-text 500
with no JSON body, and the CLI, which prints the `error` field, had nothing to
print. An agent renaming its own session got "HTTP Error 500" and no reason,
five times in one morning.

Two things are asserted here, because the bug needed both to stay hidden: the
write acts in the source's own organization, and a failure says something.
"""
from __future__ import annotations

import asyncio

import pytest


class _Req:
    def __init__(self, tmux, label):
        self.path_params = {"tmux_name": tmux}
        self._body = {"label": label}
        self.headers = {}

    async def json(self):
        return self._body


@pytest.fixture
def server(monkeypatch):
    import tools.dashboard.server as s

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.setattr(s.dashboard_db, "update_label", lambda *a, **k: None)
    monkeypatch.setattr(
        s.dashboard_db, "get_session",
        lambda name: {"graph_source_id": "src-in-anchore"})

    async def no_broadcast(*a, **k):
        return None

    monkeypatch.setattr(s.event_bus, "broadcast", no_broadcast)
    monkeypatch.setattr(s.session_monitor, "get_registry", lambda: {})
    monkeypatch.setattr(s.dao_sessions, "get_active_sessions", lambda: [])
    return s


def test_the_retitle_acts_in_the_sources_own_org(server, monkeypatch):
    seen = {}

    monkeypatch.setattr(server.graph_ops, "_resolve_source_home",
                        lambda sid, *, org: "anchore")

    def fake_update(source_id, title, *, org=None):
        seen["org"] = org

    monkeypatch.setattr(server.graph_ops, "update_source_title", fake_update)

    resp = asyncio.run(server.api_session_label(_Req("auto-x", "New label")))

    assert resp.status_code == 200
    assert seen["org"] == "anchore", (
        "the write must name the source's organization, not the caller's")


def test_a_failed_retitle_says_why_instead_of_a_bare_500(server, monkeypatch):
    """The CLI prints the JSON `error` field. A plain-text 500 leaves it with
    nothing, which is why this was reported five times with no detail."""
    monkeypatch.setattr(server.graph_ops, "_resolve_source_home",
                        lambda sid, *, org: "anchore")

    def boom(source_id, title, *, org=None):
        raise RuntimeError("cannot modify cross-org content")

    monkeypatch.setattr(server.graph_ops, "update_source_title", boom)

    resp = asyncio.run(server.api_session_label(_Req("auto-x", "New label")))

    assert resp.status_code == 502
    assert b"cannot modify cross-org content" in resp.body
    assert b"error" in resp.body


def test_a_session_with_no_graph_source_still_labels(server, monkeypatch):
    """Not every session is ingested; the label is still the session's own."""
    monkeypatch.setattr(server.dashboard_db, "get_session", lambda name: {})

    resp = asyncio.run(server.api_session_label(_Req("auto-x", "New label")))

    assert resp.status_code == 200
