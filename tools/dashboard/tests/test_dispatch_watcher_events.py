"""auto-jnm58 — event-driven dispatch watcher.

The header counters used to be recomputed (ten reads, incl. a git status
subprocess) every 5 s and broadcast unconditionally. Now each 5 s tick only
SAMPLES cheap change signals; the reads + broadcasts run when a signal moved
or the 60 s heartbeat is due. A dispatch-DB ``data_version`` move also nudges
``timeline:changed`` so the Activity/Timeline pages refetch event-driven.

These tests drive ``_DispatchWatcher.tick`` with a fake bus, a fake clock,
and stubbed signal samplers — no DB, no event loop timers.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pytest_asyncio")

from tools.dashboard import server as srv


class FakeBus:
    """Minimal EventBus: a real queue for signal frames, a broadcast log."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.broadcasts: list[tuple[str, object, bool]] = []
        self.unsubscribed = False

    def subscribe(self, client_id=None):
        return self.queue

    def unsubscribe(self, q):
        self.unsubscribed = True

    async def broadcast(self, topic, data, dedup=True):
        self.broadcasts.append((topic, data, dedup))
        return 1

    def count(self, topic):
        return sum(1 for (t, _d, _dd) in self.broadcasts if t == topic)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _make_watcher(bus, clock, *, dv, head, recompute_fn=None, reconcile_fn=None):
    dv_box = {"v": dv}
    head_box = {"v": head}
    watcher = srv._DispatchWatcher(
        bus=bus,
        data_version_fn=lambda: dv_box["v"],
        dolt_head_fn=lambda: head_box["v"],
        recompute_fn=recompute_fn,
        reconcile_fn=reconcile_fn or (lambda: _noop()),
        orphan_fn=lambda: _noop(),
        clock=clock,
        interval=5,
        heartbeat=60,
    )
    return watcher, dv_box, head_box


async def _noop():
    return None


@pytest.mark.asyncio
async def test_quiet_two_minutes_at_most_two_dispatch_broadcasts():
    bus = FakeBus()
    clock = Clock()
    calls: list[srv._WatcherSignals] = []

    async def fake_recompute(signals, now):
        calls.append(signals)
        # Real recompute forces a broadcast on the heartbeat; mirror that so
        # the dispatch-broadcast count tracks the recompute cadence.
        await bus.broadcast("dispatch", {"n": len(calls)}, dedup=False)

    watcher, _dv, _head = _make_watcher(
        bus, clock, dv=1, head="h", recompute_fn=fake_recompute)
    await watcher.prime()

    # 24 quiet ticks across two minutes (t = 5,10,...,120), no signal moves.
    for t in range(5, 121, 5):
        clock.t = float(t)
        await watcher.tick()

    assert bus.count("dispatch") <= 2, bus.broadcasts
    # And it is genuinely the heartbeat cadence: t=5 (first tick) and t=65.
    assert bus.count("dispatch") == 2
    assert bus.count("timeline:changed") == 0


@pytest.mark.asyncio
async def test_session_registry_frame_triggers_one_recompute_within_a_tick():
    bus = FakeBus()
    clock = Clock()
    calls: list[srv._WatcherSignals] = []

    async def fake_recompute(signals, now):
        calls.append(signals)

    watcher, _dv, _head = _make_watcher(
        bus, clock, dv=1, head="h", recompute_fn=fake_recompute)
    await watcher.prime()

    # Warm-up tick consumes the first-tick heartbeat.
    clock.t = 5.0
    await watcher.tick()
    assert len(calls) == 1 and calls[0].heartbeat is True

    # A single session:registry frame between heartbeats forces one recompute.
    bus.queue.put_nowait(("session:registry", {}, 1))
    clock.t = 10.0
    await watcher.tick()
    assert len(calls) == 2
    assert calls[1].heartbeat is False
    assert calls[1].sessions is True


@pytest.mark.asyncio
async def test_ten_frames_in_one_second_coalesce_to_one_recompute():
    bus = FakeBus()
    clock = Clock()
    calls: list[srv._WatcherSignals] = []

    async def fake_recompute(signals, now):
        calls.append(signals)

    watcher, _dv, _head = _make_watcher(
        bus, clock, dv=1, head="h", recompute_fn=fake_recompute)
    await watcher.prime()

    clock.t = 5.0
    await watcher.tick()  # heartbeat warm-up
    assert len(calls) == 1

    for i in range(10):
        bus.queue.put_nowait(("session:registry", {"i": i}, i + 1))
    clock.t = 6.0  # same-second burst drained in one tick
    await watcher.tick()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_data_version_move_emits_one_timeline_changed_and_recomputes():
    bus = FakeBus()
    clock = Clock()
    calls: list[srv._WatcherSignals] = []

    async def fake_recompute(signals, now):
        calls.append(signals)

    watcher, dv_box, _head = _make_watcher(
        bus, clock, dv=1, head="h", recompute_fn=fake_recompute)
    await watcher.prime()

    clock.t = 5.0
    await watcher.tick()  # heartbeat warm-up, dv unchanged
    assert bus.count("timeline:changed") == 0

    dv_box["v"] = 2
    clock.t = 10.0
    await watcher.tick()
    assert bus.count("timeline:changed") == 1
    assert calls[-1].timeline is True

    # A second distinct move nudges again (empty payload is NOT deduped away).
    dv_box["v"] = 3
    clock.t = 15.0
    await watcher.tick()
    assert bus.count("timeline:changed") == 2
    # All timeline:changed frames use dedup=False so a burst all lands.
    assert all(
        dd is False for (t, _d, dd) in bus.broadcasts if t == "timeline:changed")


@pytest.mark.asyncio
async def test_recompute_gating_skips_ungated_reads(monkeypatch):
    """A beads-only signal recomputes bead counts but not the timeline read."""
    bus = FakeBus()
    clock = Clock()
    called: set[str] = set()

    async def _fake_dispatch_data():
        called.add("dispatch_data")
        return {"active": [], "waiting": [], "waiting_total": 0, "blocked": []}

    monkeypatch.setattr(srv, "_collect_dispatch_data", _fake_dispatch_data)
    monkeypatch.setattr(srv.dao_beads, "get_bead_counts",
                        lambda: called.add("counts") or {"open_count": 3})
    monkeypatch.setattr(srv.dao_beads, "get_beads_by_label",
                        lambda label: called.add("pinned") or [])
    monkeypatch.setattr(srv, "_count_today_done",
                        lambda: called.add("today_done") or 0)
    monkeypatch.setattr(srv, "_get_pause_dict",
                        lambda: called.add("pause") or {"paused": False, "reason": None})
    monkeypatch.setattr(srv, "_get_merge_health",
                        lambda: called.add("git") or {"status": "ok"})
    monkeypatch.setattr(srv, "_count_active_sessions",
                        lambda: called.add("sessions") or 0)
    monkeypatch.setattr(srv, "_count_terminals",
                        lambda: called.add("terminals") or 0)
    monkeypatch.setattr(srv, "_count_worktrees",
                        lambda: called.add("worktree_counts") or {"with_commits": 0, "with_changes": 0})
    monkeypatch.setattr(srv, "_collect_worktree_state_rows",
                        lambda: called.add("wt_rows") or [])
    monkeypatch.setattr(srv, "_count_streams",
                        lambda: called.add("streams") or 0)
    monkeypatch.setattr(srv, "_collect_harness_usage",
                        lambda: called.add("harness") or {"harnesses": []})
    monkeypatch.setattr(srv, "_collect_plugin_badges",
                        lambda: called.add("plugins") or {})

    watcher, _dv, _head = _make_watcher(bus, clock, dv=1, head="h")

    # A pure heartbeat runs every read (seeds all caches).
    await watcher._recompute(srv._WatcherSignals(heartbeat=True), now=0.0)
    assert "today_done" in called and "counts" in called and "git" in called

    # A beads-only signal re-reads bead counts, not the timeline/session reads.
    called.clear()
    await watcher._recompute(srv._WatcherSignals(beads=True), now=5.0)
    assert "counts" in called
    assert "pinned" in called
    assert "today_done" not in called
    assert "sessions" not in called
    assert "git" not in called  # git floored to 60 s


@pytest.mark.asyncio
async def test_recompute_broadcasts_dispatch_and_nav_on_change(monkeypatch):
    bus = FakeBus()
    clock = Clock()

    state = {"open": 1}

    async def _fake_dispatch_data():
        return {"active": [], "waiting": [], "waiting_total": 0, "blocked": []}

    monkeypatch.setattr(srv, "_collect_dispatch_data", _fake_dispatch_data)
    monkeypatch.setattr(srv.dao_beads, "get_bead_counts",
                        lambda: {"open_count": state["open"]})
    monkeypatch.setattr(srv.dao_beads, "get_beads_by_label", lambda label: [])
    monkeypatch.setattr(srv, "_count_today_done", lambda: 0)
    monkeypatch.setattr(srv, "_get_pause_dict",
                        lambda: {"paused": False, "reason": None})
    monkeypatch.setattr(srv, "_get_merge_health", lambda: {"status": "ok"})
    monkeypatch.setattr(srv, "_count_active_sessions", lambda: 0)
    monkeypatch.setattr(srv, "_count_terminals", lambda: 0)
    monkeypatch.setattr(srv, "_count_worktrees",
                        lambda: {"with_commits": 0, "with_changes": 0})
    monkeypatch.setattr(srv, "_collect_worktree_state_rows", lambda: [])
    monkeypatch.setattr(srv, "_count_streams", lambda: 0)
    monkeypatch.setattr(srv, "_collect_harness_usage", lambda: {"harnesses": []})
    monkeypatch.setattr(srv, "_collect_plugin_badges", lambda: {})

    watcher, _dv, _head = _make_watcher(bus, clock, dv=1, head="h")

    # First heartbeat broadcasts dispatch + nav.
    await watcher._recompute(srv._WatcherSignals(heartbeat=True), now=0.0)
    assert bus.count("dispatch") == 1
    assert bus.count("nav") == 1

    # A beads recompute with no actual change does NOT re-broadcast dispatch.
    await watcher._recompute(srv._WatcherSignals(beads=True), now=5.0)
    assert bus.count("dispatch") == 1

    # A real bead-count change re-broadcasts nav (and dispatch, same gate).
    state["open"] = 7
    await watcher._recompute(srv._WatcherSignals(beads=True), now=10.0)
    assert bus.count("nav") == 2
