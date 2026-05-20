"""Tests for CrossTalk log retrieval via API and CLI fallback."""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from starlette.testclient import TestClient


class TestCrossTalkLogCLI(unittest.TestCase):
    """CLI log retrieval should fall back to the dashboard API in containers."""

    @mock.patch(
        "tools.graph.cli.os.environ",
        {"CROSSTALK_TOKEN": "tok", "GRAPH_API": "https://localhost:8080"},
    )
    def test_uses_api_when_local_auth_db_missing(self):
        body = {
            "messages": [{
                "timestamp": 1712000000,
                "sender_label": "Peer",
                "sender_session": "auto-peer",
                "target_session": "auto-self",
                "message": "hello from api",
                "delivered": 1,
            }],
        }

        def mock_urlopen(req, timeout=None, context=None):
            self.assertEqual(req.full_url, "https://localhost:8080/api/crosstalk/log?limit=5&session=auto-peer&since=10m")
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.headers["Authorization"], "Bearer tok")
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps(body).encode()
            return resp

        args = argparse.Namespace(limit=5, session="auto-peer", since="10m")
        with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
            f = io.StringIO()
            with redirect_stdout(f):
                from tools.graph.cli import cmd_crosstalk
                cmd_crosstalk(args)
        output = f.getvalue()
        assert "Peer" in output
        assert "hello from api" in output

    @mock.patch(
        "tools.graph.cli.os.environ",
        {"CROSSTALK_TOKEN": "tok", "GRAPH_API": "https://localhost:8080"},
    )
    def test_no_messages_prints_empty_state(self):
        resp = mock.MagicMock()
        resp.read.return_value = b'{"messages": []}'
        args = argparse.Namespace(limit=30, session=None, since=None)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            f = io.StringIO()
            with redirect_stdout(f):
                from tools.graph.cli import cmd_crosstalk
                cmd_crosstalk(args)
        assert "No CrossTalk messages found" in f.getvalue()


class TestCrossTalkLogAPI:
    """Server route for CrossTalk log retrieval."""

    def test_returns_messages_from_auth_db(self, monkeypatch):
        from tools.dashboard import server as server_mod

        monkeypatch.setattr(server_mod.auth_db, "resolve_token", lambda _h: "auto-sender")
        captured: dict = {}

        def fake_get_messages(*, limit, since, session):
            captured["limit"] = limit
            captured["since"] = since
            captured["session"] = session
            return [{
                "timestamp": 1712000000,
                "sender_label": "Peer",
                "sender_session": "auto-peer",
                "target_session": "auto-target",
                "message": "hello",
                "delivered": 1,
            }]

        monkeypatch.setattr(server_mod.auth_db, "get_messages", fake_get_messages)

        with TestClient(server_mod.app) as client:
            r = client.get(
                "/api/crosstalk/log?limit=7&session=auto-peer&since=10m",
                headers={"Authorization": "Bearer test-token"},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["messages"]) == 1
        assert body["messages"][0]["message"] == "hello"
        assert captured["limit"] == 7
        assert captured["session"] == "auto-peer"
        assert captured["since"] is not None

    def test_rejects_invalid_since(self, monkeypatch):
        from tools.dashboard import server as server_mod

        monkeypatch.setattr(server_mod.auth_db, "resolve_token", lambda _h: "auto-sender")

        with TestClient(server_mod.app) as client:
            r = client.get(
                "/api/crosstalk/log?since=not-a-duration",
                headers={"Authorization": "Bearer test-token"},
            )

        assert r.status_code == 400
        assert r.json()["error"] == "since must be a duration like 30m, 1h, or 2d"

