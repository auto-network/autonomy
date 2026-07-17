"""Clean-machine degradation: no bd binary, no dolt server (H5, DEPLOY.md).

A deployment without the beads toolchain is a supported empty state.
These pin the two layers that used to 500: run_cli on a missing binary,
and the pymysql beads DAO on an unreachable Dolt server.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.dashboard import server
from tools.dashboard.dao import beads as dao_beads


def test_run_cli_missing_binary_soft_fails():
    stdout, stderr, rc = asyncio.run(
        server.run_cli(["definitely-not-a-real-binary-xyz", "arg"])
    )
    assert rc == 127
    assert stdout == ""
    assert "definitely-not-a-real-binary-xyz" in stderr


def test_run_cli_json_missing_binary_returns_error_shape():
    result = asyncio.run(server.run_cli_json(["definitely-not-a-real-binary-xyz"]))
    assert isinstance(result, dict)
    assert result["returncode"] == 127
    assert "error" in result


def test_run_cli_json_missing_binary_degrades_to_empty():
    """List-shaped bd endpoints hand the frontend a real empty list."""
    result = asyncio.run(
        server.run_cli_json(["definitely-not-a-real-binary-xyz"], empty=[])
    )
    assert result == []


def test_api_beads_list_empty_when_bd_missing(monkeypatch):
    """The API endpoint itself returns [], not a 200-with-error object."""
    import json

    async def missing_binary_run_cli(cmd, timeout=30, stdin_data=None):
        return "", f"{cmd[0]}: not found", 127

    monkeypatch.setattr(server, "run_cli", missing_binary_run_cli)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)

    class _Req:
        pass

    for endpoint in (server.api_beads_list, server.api_beads_ready):
        response = asyncio.run(endpoint(_Req()))
        assert response.status_code == 200
        assert json.loads(response.body) == []


@pytest.fixture()
def _dead_dolt(monkeypatch):
    """Point the DAO at a port nothing listens on, with a fast timeout."""
    monkeypatch.setattr(dao_beads, "_DOLT_HOST", "127.0.0.1")
    monkeypatch.setattr(dao_beads, "_DOLT_PORT", 1)  # reserved, never open
    monkeypatch.setattr(dao_beads, "_unreachable_logged", False)
    # Thread-local cached connection would bypass the connect; clear it.
    monkeypatch.setattr(dao_beads, "_local", type(dao_beads._local)())


def test_beads_dao_degrades_to_empty_when_dolt_unreachable(_dead_dolt, caplog):
    with caplog.at_level("WARNING", logger="tools.dashboard.dao.beads"):
        assert dao_beads.get_beads_by_label("pinned") == []
        assert dao_beads.get_open_beads() == []
        assert dao_beads.get_bead("auto-xyz") is None
        assert dao_beads.get_bead_title_priority(["auto-xyz"]) == {}
        assert dao_beads.get_bead_counts() == {}
        assert dao_beads.get_dispatch_beads() == {
            "approved_waiting": [],
            "approved_blocked": [],
        }
    # Logged once, not once per call.
    warnings = [r for r in caplog.records if "dolt unreachable" in r.message]
    assert len(warnings) == 1
