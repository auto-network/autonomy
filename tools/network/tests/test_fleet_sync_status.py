"""Fleet sync status reader — frontier, staleness, and per-peer state."""

from __future__ import annotations

import sqlite3
import time

import pytest

from tools.graph.db import GraphDB, resolve_caller_db_path
from tools.network import fleet_sync_status as st


@pytest.fixture
def personal_db(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    yield str(resolve_caller_db_path(None))
    GraphDB.close_all_pooled()


def _activate(db):
    with GraphDB(db) as g:
        g.activate_fleet_sync_writers("aa" * 32)


def _insert(db, pub, *, online, last_success_ns, wm, applied=0, retries=0, err=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fleet_sync_peer_state (machine_public_key,roster_epoch,online,"
        "last_success_ns,peer_watermark,transactions_applied,retries,last_error_code,"
        "updated_at_ns) VALUES (?,?,?,?,?,?,?,?,?)",
        (pub, "epoch1", 1 if online else 0, last_success_ns, wm, applied, retries, err, 0))
    conn.commit()
    conn.close()


def test_unactivated_store_is_unavailable(personal_db):
    s = st.read_status(personal_db)
    assert s["available"] is False
    assert "not activated" in s["reason"]


def test_frontier_is_min_online_watermark(personal_db):
    _activate(personal_db)
    now = time.time_ns()
    _insert(personal_db, "bb" * 32, online=True, last_success_ns=now, wm=1500)
    _insert(personal_db, "cc" * 32, online=True, last_success_ns=now, wm=1400)
    _insert(personal_db, "dd" * 32, online=False, last_success_ns=now, wm=1200)
    s = st.read_status(personal_db)
    assert s["frontier"] == 1400  # min among ONLINE peers, not the offline 1200
    assert s["stale"] is False
    assert s["online_count"] == 2 and s["peer_count"] == 3


def test_no_online_peer_is_stale_last_established(personal_db):
    _activate(personal_db)
    now = time.time_ns()
    _insert(personal_db, "bb" * 32, online=False, last_success_ns=now, wm=1500)
    _insert(personal_db, "cc" * 32, online=False, last_success_ns=now, wm=1200)
    s = st.read_status(personal_db)
    assert s["stale"] is True
    assert s["frontier"] == 1200  # min established watermark


def test_never_synced_peer_has_no_frontier(personal_db):
    _activate(personal_db)
    _insert(personal_db, "bb" * 32, online=False, last_success_ns=None, wm=None)
    s = st.read_status(personal_db)
    assert s["frontier"] is None and s["stale"] is True


def test_human_render_names_errors_and_staleness(personal_db):
    _activate(personal_db)
    now = time.time_ns()
    _insert(personal_db, "cc" * 32, online=False, last_success_ns=now, wm=1200,
            err="watermark-stale")
    text = st.format_human(st.read_status(personal_db), now_ns=now)
    assert "stale" in text and "watermark-stale" in text
