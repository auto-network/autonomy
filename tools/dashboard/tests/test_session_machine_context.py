"""The machine context a session is launched with (bead auto-n3rxb):
local launches name this machine as home; a remote launch names the
machine the session:control handshake proved; the env mirrors it."""

from __future__ import annotations

import pytest

from tools.dashboard import server, session_presence
from tools.dashboard.dao import dashboard_db

HERE = session_presence.LocalMachine("b1" * 32, "b2" * 32)
HOME_PUB = "a1" * 32


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    dashboard_db.init_db(tmp_path / "dashboard.db")
    monkeypatch.setattr(session_presence, "local_machine", lambda: HERE)
    monkeypatch.setattr(session_presence, "_machine_names",
                        lambda: {"b2" * 32: "sjc-2", "a2" * 32: "home"})
    monkeypatch.setattr(session_presence, "_active_roster",
                        lambda: {HERE.machine_pub: "b2" * 32, HOME_PUB: "a2" * 32})


def test_a_local_session_is_home_here(ctx):
    dashboard_db.upsert_session("auto-1", "container", "p")
    where = server._session_machine_context("auto-1")
    assert where == {"machine": "sjc-2", "machine_pub": HERE.machine_pub,
                     "home_machine": "sjc-2", "home_machine_pub": HERE.machine_pub,
                     "launched_by": "local", "remote": False}


def test_a_remotely_launched_session_names_its_home(ctx):
    dashboard_db.upsert_session("auto-2", "container", "p")
    dashboard_db.set_launch_provenance(
        "auto-2", launched_by=f"machine:{HOME_PUB}", home_machine=HOME_PUB,
        launch_op_id="0f" * 16)
    where = server._session_machine_context("auto-2")
    assert where["remote"] is True and where["home_machine"] == "home"
    assert server._machine_env(where) == {
        "AUTONOMY_MACHINE": "sjc-2",
        "AUTONOMY_MACHINE_PUB": HERE.machine_pub,
        "AUTONOMY_HOME_MACHINE": "home",
        "AUTONOMY_LAUNCHED_BY": f"machine:{HOME_PUB}",
    }


def test_no_fleet_identity_means_no_context_and_no_env(monkeypatch):
    monkeypatch.setattr(session_presence, "local_machine", lambda: None)
    assert server._session_machine_context("auto-1") is None
    assert server._machine_env(None) == {}
