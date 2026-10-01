"""2026-10-01: on a cold start (no hand-off snapshot restored), every live
session's transcript is read once, in order, on one start-up thread, instead
of by whichever catch-ups and first tail reads arrive together."""
from __future__ import annotations

import asyncio
import json

import pytest

from tools.dashboard import server, session_monitor
from tools.dashboard.dao import dashboard_db
from tools.dashboard.tests.conftest import MOCK_ENTRIES


@pytest.fixture
def fresh():
    server._recon_cache.clear()
    server._recon_stats.update(full=0, incremental=0)
    yield
    server._recon_cache.clear()


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "sessions" / "u" / "abc.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in MOCK_ENTRIES))
    return path


def _row(path, name="host-a", activity=1.0):
    return {"tmux_name": name, "jsonl_path": str(path), "file_offset": path.stat().st_size,
            "session_uuids": json.dumps([path.stem]), "last_activity": activity}


def test_warm_one_builds_state1_and_leaves_a_checkpoint_at_file_offset(transcript, fresh):
    end = transcript.stat().st_size
    entry = server._warm_one_tail_state(_row(transcript))
    assert entry["offset"] == end and entry["path"] == str(transcript)
    assert server._recon_stats == {"full": 1, "incremental": 0}
    server._reconstruct_read_state([("abc", transcript)], server.CLAUDE_HARNESS,
                                   upto_file="abc", upto_off=end)
    assert server._recon_stats == {"full": 1, "incremental": 1}
    assert server._recon_last.info["bytes"] == 0


def test_cold_warm_up_reads_each_cold_session_once_and_offers_it(tmp_path, transcript, fresh, monkeypatch):
    other = tmp_path / "sessions" / "v" / "def.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text(transcript.read_text())
    rows = [_row(transcript, "host-old", 1.0), _row(other, "host-new", 2.0), _row(other, "host-warm", 3.0)]
    monkeypatch.setattr(dashboard_db, "get_live_sessions", lambda: rows)
    offered = []
    monkeypatch.setattr(server.session_monitor, "is_tail_state_warm", lambda name: name == "host-warm")
    monkeypatch.setattr(server.session_monitor, "add_restored_tail_state",
                        lambda name, entry: offered.append((name, entry["offset"])))

    async def go():
        result = await server._cold_warm_tail_states(set())
        await asyncio.sleep(0)            # let the thread-safe callbacks run
        return result
    warmed, read = asyncio.run(go())
    assert warmed == 2
    assert [name for name, _ in offered] == ["host-new", "host-old"]     # most recent first
    assert read == transcript.stat().st_size + other.stat().st_size


def test_monitor_ignores_an_offer_for_a_session_that_warmed_itself():
    mon = session_monitor.SessionMonitor.__new__(session_monitor.SessionMonitor)
    mon._restored_tail_states = {}
    warm = session_monitor._TailState()
    warm.task_tracker_warmed = True
    mon._tail_states = {"host-warm": warm}
    mon.add_restored_tail_state("host-warm", {"offset": 1})
    mon.add_restored_tail_state("host-cold", {"offset": 2})
    assert mon._restored_tail_states == {"host-cold": {"offset": 2}}
