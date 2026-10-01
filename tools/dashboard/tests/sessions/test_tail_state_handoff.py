"""2026-10-01: the two tail states survive a hot reload.

State 1 is each live session's tail state at the offset it has published;
State 2 is the catch-up checkpoint at the cursor of the last catch-up. Both
lived only in memory, so a new worker re-parsed every live transcript from
byte 0 to warm its task tracker, and each transcript's first catch-up
replayed from byte 0. The old worker snapshots both at hand-off; the new one
restores them before the session monitor starts."""
from __future__ import annotations

import json
import os
import time

import pytest

from tools.dashboard import server, session_monitor
from tools.dashboard.dao import dashboard_db
from tools.dashboard.tests.conftest import MOCK_ENTRIES


@pytest.fixture
def fresh(monkeypatch):
    server._recon_cache.clear()
    server._recon_stats.update(full=0, incremental=0)
    monkeypatch.setattr(server, "_task_state_tracker", server.TaskStateTracker())
    yield
    server._recon_cache.clear()


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "sessions" / "u" / "abc.jsonl"
    path.parent.mkdir(parents=True)
    lines = [json.dumps(e) + "\n" for e in MOCK_ENTRIES]
    path.write_text("".join(lines))
    p_snap = sum(len(l.encode()) for l in lines[: len(lines) // 2])
    return path, p_snap, path.stat().st_size


def _state1_at(path, offset):
    """The old worker's exported State 1 at ``offset`` (same pipeline as the tail)."""
    st = server._reconstruct_read_state_uncounted(
        [("abc", path)], server.CLAUDE_HARNESS, upto_file="abc", upto_off=offset)
    server._recon_cache.clear()
    stat = os.stat(path)
    return {
        "path": str(path), "offset": offset, "generation": (stat.st_dev, stat.st_ino),
        "parse_ctx": st["parse_ctx"], "postprocess_state": st["postprocess_state"],
        "last_enqueue_content": st["last_enqueue_content"],
        "agent_descriptions": st["agent_descriptions"],
        "claimed_subagents": st["claimed_subagents"],
        "tracker": st["tracker"]._sessions.get("_reconstruct"),
    }


def _snapshot(tmp_path, sessions, recon=()):
    snap = tmp_path / "tail_state.snapshot"
    server._write_tail_snapshot(snap, {"version": server._TAIL_SNAPSHOT_VERSION,
                                       "written_at": time.time(),
                                       "sessions": sessions, "recon": list(recon)})
    return snap


def _row(monkeypatch, path, file_offset):
    row = {"tmux_name": "host-a", "jsonl_path": str(path), "file_offset": file_offset,
           "session_uuids": json.dumps(["abc"])}
    monkeypatch.setattr(dashboard_db, "get_session", lambda name: row if name == "host-a" else None)
    return row


def test_restore_advances_state1_to_file_offset_and_keeps_state2_at_p_snap(
        tmp_path, transcript, fresh, monkeypatch):
    path, p_snap, end = transcript
    _row(monkeypatch, path, end)          # the old worker published to the end
    snap = _snapshot(tmp_path, {"host-a": _state1_at(path, p_snap)})
    expected = server._reconstruct_read_state_uncounted(
        [("abc", path)], server.CLAUDE_HARNESS, upto_file="abc", upto_off=end)
    server._recon_cache.clear()
    server._recon_stats.update(full=0, incremental=0)

    ready, _ = server._restore_tail_snapshot(snap)
    assert not snap.exists()
    got = ready["host-a"]
    assert got["offset"] == end
    assert got["last_enqueue_content"] == expected["last_enqueue_content"]
    assert got["agent_descriptions"] == expected["agent_descriptions"]
    assert server._recon_stats == {"full": 0, "incremental": 1}   # only P_snap..end parsed

    (entry,) = server._recon_cache.values()
    assert entry["frontier"] == {"abc": p_snap}
    server._recon_stats.update(full=0, incremental=0)
    server._reconstruct_read_state(
        [("abc", path)], server.CLAUDE_HARNESS, upto_file="abc", upto_off=p_snap)
    assert server._recon_stats == {"full": 0, "incremental": 1}
    assert server._recon_last.info["bytes"] == 0


@pytest.mark.parametrize("change", ["other_path", "db_behind"])
def test_a_changed_file_or_offset_is_not_carried_over(tmp_path, transcript, fresh, monkeypatch, change):
    path, p_snap, end = transcript
    row = _row(monkeypatch, path, end)
    if change == "other_path":
        row["jsonl_path"] = str(path) + ".other"
    else:
        row["file_offset"] = p_snap - 1
    snap = _snapshot(tmp_path, {"host-a": _state1_at(path, p_snap)})
    ready, _ = server._restore_tail_snapshot(snap)
    assert ready == {}


def test_stale_or_foreign_snapshots_are_ignored(tmp_path, fresh):
    snap = tmp_path / "tail_state.snapshot"
    server._write_tail_snapshot(snap, {"version": server._TAIL_SNAPSHOT_VERSION,
                                       "written_at": time.time() - 3600,
                                       "sessions": {"x": {}}, "recon": []})
    assert server._restore_tail_snapshot(snap) == ({}, 0)
    assert not snap.exists()
    assert server._restore_tail_snapshot(snap) == ({}, 0)


def test_parser_changes_are_detected():
    assert server._tail_parser_changed(["/app/tools/dashboard/session_harness.py"])
    assert server._tail_parser_changed(["tools/dashboard/session_monitor.py"])
    assert not server._tail_parser_changed(["tools/dashboard/server.py", "tools/graph/cli.py"])
    assert not server._tail_parser_changed(None)


def _monitor():
    mon = session_monitor.SessionMonitor.__new__(session_monitor.SessionMonitor)
    mon._tail_states, mon._tracks = {}, {}
    mon._restored_tail_states, mon._install_restored_tracker = {}, None
    return mon


def test_monitor_adopts_an_exact_match_once_and_skips_warm_up(transcript):
    path, p_snap, _ = transcript
    stat = os.stat(path)
    installed = []
    mon = _monitor()
    mon.install_restored_tail_states({"host-a": {
        "path": str(path), "offset": p_snap, "generation": (stat.st_dev, stat.st_ino),
        "parse_ctx": {"k": 1}, "postprocess_state": {"p": 2}, "last_enqueue_content": "q",
        "agent_descriptions": {"t": "d"}, "claimed_subagents": {"m"}, "tracker": "slice",
    }}, lambda name, s: installed.append((name, s)))
    ts = session_monitor._TailState()
    mon._adopt_restored_tail_state("host-a", {"jsonl_path": str(path), "file_offset": p_snap}, ts)
    assert (ts.parse_ctx, ts.postprocess_state, ts.last_enqueue_content) == ({"k": 1}, {"p": 2}, "q")
    assert ts.task_tracker_warmed and (ts.state_path, ts.state_offset) == (str(path), p_snap)
    assert installed == [("host-a", "slice")]
    assert mon._restored_tail_states == {}


def test_monitor_drops_a_mismatch_and_warms_up_as_before(transcript):
    path, p_snap, _ = transcript
    mon = _monitor()
    mon.install_restored_tail_states({"host-a": {
        "path": str(path), "offset": p_snap, "generation": None, "tracker": "slice"}},
        lambda *a: pytest.fail("tracker installed for a mismatch"))
    ts = session_monitor._TailState()
    mon._adopt_restored_tail_state("host-a", {"jsonl_path": str(path), "file_offset": p_snap + 1}, ts)
    assert not ts.task_tracker_warmed and ts.state_offset is None


def test_monitor_exports_committed_states_and_passes_unadopted_ones_through(transcript):
    path, p_snap, _ = transcript
    mon = _monitor()
    ts = session_monitor._TailState()
    ts.state_path, ts.state_offset, ts.last_enqueue_content = str(path), p_snap, "q"
    mon._tail_states = {"host-a": ts, "host-idle": session_monitor._TailState()}
    mon._restored_tail_states = {"host-b": {"offset": 7, "tracker": None}}
    out = mon.export_tail_states()
    assert set(out) == {"host-a", "host-b"}
    assert out["host-a"]["offset"] == p_snap and out["host-a"]["last_enqueue_content"] == "q"
