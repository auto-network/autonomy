"""End-to-end delivery test: setting write → EventBus subscriber.

Drives the same path that an SSE client would: a subscriber attaches to
the dashboard EventBus, the test POSTs a Setting write through the live
ASGI app, and the subscriber must receive a ``setting.changed`` event
within 2 seconds. Mirrors the SSE-level test pattern in
``test_sse_delivery.py`` but stays in-process for portability — the SSE
transport itself is exercised by ``test_event_bus_replay.py``.

Bead: auto-p5rbu.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest

pytest.importorskip("pytest_asyncio")

from tools.dashboard.event_bus import EventBus
from tools.graph import schemas
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


_DASHBOARD_TABLE_DDL = """CREATE TABLE IF NOT EXISTS tmux_sessions (
    tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
    type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
    bead_id TEXT, created_at REAL NOT NULL, is_live INTEGER DEFAULT 1,
    file_offset INTEGER DEFAULT 0, last_activity REAL,
    last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
    context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
    topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
    nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
    nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
    dispatch_nag INTEGER DEFAULT 0,
    resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
    curr_jsonl_file TEXT
)"""


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture
def isolated_dashboard(tmp_path, monkeypatch):
    """Reload dashboard with a tmp-pathed graph DB + dashboard DB."""
    graph_path = tmp_path / "graph.db"
    dash_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("GRAPH_DB", str(graph_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("DASHBOARD_DB", str(dash_path))
    monkeypatch.setenv("DASHBOARD_EVENT_BUS_STATE", str(tmp_path / "bus.state"))

    conn = sqlite3.connect(str(dash_path))
    conn.execute(_DASHBOARD_TABLE_DDL)
    conn.commit()
    conn.close()

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)
    return server_mod


@pytest.mark.asyncio
async def test_setting_write_delivers_event_to_subscriber(isolated_dashboard):
    """POST a Setting write → subscriber receives setting.changed in <2s."""
    server_mod = isolated_dashboard

    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.delivery"
        schema_revision = 1
    schemas.register_schema("autonomy.test.delivery", 1, V1)

    bus = EventBus()
    server_mod.event_bus = bus
    queue = bus.subscribe()

    transport = httpx.ASGITransport(app=server_mod.app)
    start = time.monotonic()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/graph/setting", json={
            "set_id": "autonomy.test.delivery",
            "schema_revision": 1,
            "key": "delivery-k",
            "payload": {"x": 1},
        })
    assert resp.status_code == 201, resp.text

    # Drain bus queue (same delivery channel SSE clients use) within 2s.
    deadline = start + 2.0
    found = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            topic, data, seq = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        # subscribe() emits cached topics with seq=0 — those are not real events.
        if topic == "setting.changed" and seq > 0:
            found = data
            break

    assert found is not None, "setting.changed not delivered within 2s"
    assert found["operation"] == "write"
    assert found["set_id"] == "autonomy.test.delivery"
    assert found["key"] == "delivery-k"
    assert found["schema_revision"] == 1
    assert found["publication_state"] == "raw"
    assert found["deprecated"] is False
