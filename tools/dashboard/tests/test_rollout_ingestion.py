"""L1/L2.A tests for the rollout-ingestion state machine (bead auto-suvcp).

Each test pins a model-checked property or finding from
tools/dashboard/TLA/RolloutIngestion.tla — cross-references cited per
test. Real DB (full schema via init_db), real files; drains run as real
asyncio tasks through the per-session gate.

The primary CalStartupStall regression lives in
test_rollout_ingestion_regression.py; this module covers the rest of the
bead's L1 + L2.A matrix.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

pytest.importorskip("inotify_simple")
pytest.importorskip("pytest_asyncio")


def _meta_line(uuid: str, ts: str = "2026-08-10T12:00:00Z", **payload_extra) -> str:
    payload = {
        "id": uuid,
        "timestamp": ts,
        "cwd": "/workspace/repo",
        "originator": "codex_cli_rs",
        "cli_version": "0.21.0",
        "source": "exec",
    }
    payload.update(payload_extra)
    return json.dumps({
        "timestamp": ts, "type": "session_meta", "payload": payload,
    })


def _fork_meta_line(uuid: str, ts: str = "2026-08-10T12:00:05Z") -> str:
    return json.dumps({
        "timestamp": ts,
        "type": "session_meta",
        "payload": {
            "id": uuid,
            "timestamp": ts,
            "forked_from_id": "parent-uuid",
            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
        },
    })


def _msg_line(text: str, ts: str = "2026-08-10T12:00:01.000Z") -> str:
    return json.dumps({
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text},
    })


class _CapturingBus:
    def __init__(self) -> None:
        self.broadcasts: list[tuple[str, dict]] = []

    async def broadcast(self, topic: str, payload, **_kw) -> None:
        self.broadcasts.append((topic, payload))

    def update_cache(self, topic: str, payload) -> None:
        pass

    def message_texts(self, session: str) -> list[str]:
        out = []
        for topic, payload in self.broadcasts:
            if topic == "session:messages" and payload.get("session_id") == session:
                for e in payload.get("entries") or []:
                    if isinstance(e.get("content"), str):
                        out.append(e["content"])
        return out


def _db_row(db_path: Path, tmux_name: str) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh dashboard.db + reloaded modules + graph isolation."""
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    monkeypatch.setenv("DASHBOARD_AGENT_RUNS_DIR", str(tmp_path / "agent-runs"))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    from tools.dashboard import feature_flags as ff_mod
    monkeypatch.setattr(ff_mod, "is_enabled", lambda _n, default=False: False)

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)

    class Env:
        pass

    e = Env()
    e.tmp_path = tmp_path
    e.db_path = db_path
    e.db = db_mod
    e.sm = sm_mod
    e.bus = _CapturingBus()

    def make_monitor():
        mon = sm_mod.SessionMonitor()
        mon._init_inotify()
        assert mon._use_inotify
        mon._event_bus = e.bus
        return mon

    e.make_monitor = make_monitor

    def make_session(tmux_name: str, sessions_dir: Path, **kw):
        sessions_dir.mkdir(parents=True, exist_ok=True)
        db_mod.insert_session(
            tmux_name=tmux_name,
            session_type="container",
            project="autonomy",
            harness="codex",
            resolution_dir=str(sessions_dir),
            **kw,
        )

    e.make_session = make_session
    return e


async def _settle(mon, rounds: int = 6):
    """Let drain tasks / done-callbacks run to quiescence."""
    for _ in range(rounds):
        await asyncio.sleep(0.05)
        busy = any(g.busy or g.needs_drain for g in mon._session_gates.values())
        if not busy:
            break


# ── L1: provenance and identity ────────────────────────────────────────


@pytest.mark.asyncio
async def test_n1_late_armed_fork_is_characterized_never_trusted(env):
    """N1: 'armed before this file existed' + 'first CREATE' is NOT enough —
    a subagent forked after a late arming satisfies both. Birth trust
    additionally requires the registration snapshot to be EMPTY."""
    name = "auto-n1"
    sdir = env.tmp_path / name / "sessions"
    sdir.mkdir(parents=True)
    # The parent's rollout is already on disk when the watch arms.
    parent = sdir / "rollout-2026-08-10T11-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    parent.write_text(_meta_line("aaaaaaaa-1111-1111-1111-111111111111",
                                 "2026-08-10T11:00:00Z") + "\n")
    env.make_session(name, sdir)
    mon = env.make_monitor()
    mon._add_dir_watch(name, str(sdir))   # snapshot: NON-empty

    # A subagent fork is created AFTER arming — empty at CREATE time, and
    # it IS the first CREATE observed on this watch epoch.
    fork = sdir / "rollout-2026-08-10T12-00-00-bbbbbbbb-2222-2222-2222-222222222222.jsonl"
    fork.touch()
    mon.observe_rollout(name, fork, source="IN_CREATE", create_event=True)

    track = mon._tracks[(name, str(fork))]
    assert track.state == "CHARACTERIZING", "late-armed fork must never be trusted"
    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] != str(fork)

    # When the fork's header completes, classification ignores it.
    fork.write_text(_fork_meta_line("bbbbbbbb-2222-2222-2222-222222222222") + "\n")
    mon._classify_and_step(track)
    assert track.state == "IGNORED"
    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] != str(fork), "a verified child can never link"
    await _settle(mon)   # let the parent's own link/drain tasks finish


@pytest.mark.asyncio
async def test_n5_already_linked_path_reattaches_without_cas_churn(env):
    """N5: promoting the file the row ALREADY links is always a persisted
    re-attach — offset preserved, track reaches STREAMING, no CAS churn."""
    name = "auto-n5"
    sdir = env.tmp_path / name / "sessions"
    m1 = sdir / "rollout-2026-08-10T11-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    env.make_session(name, sdir)
    m1.write_text(_meta_line("aaaaaaaa-1111-1111-1111-111111111111",
                             "2026-08-10T11:00:00Z") + "\n"
                  + _msg_line("n5 first response") + "\n")
    env.db.update_jsonl_link(name, session_uuid=m1.stem, jsonl_path=str(m1))
    offset_before = m1.stat().st_size
    env.db.update_tail_state(name, file_offset=offset_before)

    mon = env.make_monitor()
    # A reconciliation-style re-observation of the row's own linked path.
    mon.observe_rollout(name, m1, source="reconciliation")
    await _settle(mon)

    track = mon._tracks[(name, str(m1))]
    assert track.state == "STREAMING"
    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(m1)
    assert row["file_offset"] == offset_before, "re-attach must preserve the offset"
    assert row["jsonl_generation"], "re-attach backfills the generation identity"
    # No re-publication of already-acked content.
    assert env.bus.message_texts(name) == []


@pytest.mark.asyncio
async def test_n2_empty_successor_does_not_supersede(env):
    """N2 refinement: an EMPTY successor may be quarantined and never
    linkable; bare existence must not close the contentful main."""
    name = "auto-n2"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    m1 = sdir / "rollout-2026-08-10T11-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    m1.write_text(_meta_line("aaaaaaaa-1111-1111-1111-111111111111",
                             "2026-08-10T11:00:00Z") + "\n"
                  + _msg_line("m1 content line") + "\n")
    # A newer successor EXISTS but is empty.
    m2 = sdir / "rollout-2026-08-10T12-00-00-bbbbbbbb-2222-2222-2222-222222222222.jsonl"
    m2.touch()

    mon = env.make_monitor()
    mon.observe_rollout(name, m1, source="reconciliation")
    await _settle(mon)

    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(m1), (
        "a contentful main must promote despite an empty newer file"
    )
    assert row["file_offset"] == m1.stat().st_size
    assert "m1 content line" in "".join(env.bus.message_texts(name))


@pytest.mark.asyncio
async def test_quarantine_is_time_driven_and_reopens_on_progress(env):
    """T110 + B3/A6: a silent incomplete track expires from the TIME-driven
    tick (no further byte required); terminal tracks re-open only on
    observable progress."""
    name = "auto-quar"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    empty = sdir / "rollout-2026-08-10T12-00-00-cccccccc-3333-3333-3333-333333333333.jsonl"
    empty.touch()

    mon = env.make_monitor()
    mon.observe_rollout(name, empty, source="reconciliation")
    track = mon._tracks[(name, str(empty))]
    assert track.state == "CHARACTERIZING"
    assert track.characterize_deadline is not None

    # Deadline passes with no further byte; the tick must fire the expiry.
    track.characterize_deadline = time.time() - 1
    await mon.reconciliation_tick()
    assert track.state == "CLOSED"
    assert track.close_reason == "quarantined"

    # No progress → the next tick leaves it terminal (stat-only, A6).
    await mon.reconciliation_tick()
    assert track.state == "CLOSED"

    # Observable progress (the header finally lands) → reopen → promote.
    empty.write_text(_meta_line("cccccccc-3333-3333-3333-333333333333") + "\n"
                     + _msg_line("slow starter woke up") + "\n")
    await mon.reconciliation_tick()
    await _settle(mon)
    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(empty), (
        "B3: a slow startup must not be permanently unadoptable"
    )


@pytest.mark.asyncio
async def test_duplicate_observations_dedup_on_one_track(env):
    """Duplicate scans dedup into one track; repeated observation of a
    streaming file publishes nothing twice."""
    name = "auto-dedup"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    m1 = sdir / "rollout-2026-08-10T11-00-00-dddddddd-4444-4444-4444-444444444444.jsonl"
    m1.write_text(_meta_line("dddddddd-4444-4444-4444-444444444444") + "\n"
                  + _msg_line("dedup content") + "\n")

    mon = env.make_monitor()
    for _ in range(3):
        mon.observe_rollout(name, m1, source="reconciliation")
    await _settle(mon)
    assert len([k for k in mon._tracks if k[0] == name]) == 1
    texts = env.bus.message_texts(name)
    assert texts.count("dedup content") == 1
    row = _db_row(env.db_path, name)
    assert row["entry_count"] == 2


def test_wd_epoch_invalidated_on_release(env):
    """Rule 8/D3: releasing a track watch bumps the epoch and removes the
    inode dispatch mapping, so a queued stale event can never dispatch
    into whichever track later recycles the wd."""
    name = "auto-wd"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    f = sdir / "rollout-2026-08-10T12-00-00-eeeeeeee-5555-5555-5555-555555555555.jsonl"
    f.touch()

    mon = env.make_monitor()
    mon.observe_rollout(name, f, source="reconciliation")
    track = mon._tracks[(name, str(f))]
    assert track.wd is not None
    wd, epoch = track.wd, track.wd_epoch
    inode_key = mon._wd_to_inode[wd]
    assert ("track", name, str(f)) in mon._inode_watches[inode_key]["subscribers"]

    mon._release_track_watch(track)
    assert track.wd is None
    assert track.wd_epoch > epoch
    assert wd not in mon._wd_to_inode
    assert inode_key not in mon._inode_watches


# ── L2.A: ordered handover and rollover (tests 9-13) ───────────────────


def _write_chain_pair(env, name):
    """m1 linked-but-UNDRAINED with 2 content lines; m2 on disk with 2."""
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    m1 = sdir / "rollout-2026-08-10T11-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    m1.write_text(_meta_line("aaaaaaaa-1111-1111-1111-111111111111",
                             "2026-08-10T11:00:00Z") + "\n"
                  + _msg_line("m1-line-1") + "\n"
                  + _msg_line("m1-line-2") + "\n")
    env.db.update_jsonl_link(name, session_uuid=m1.stem, jsonl_path=str(m1))
    m2 = sdir / "rollout-2026-08-10T12-00-00-bbbbbbbb-2222-2222-2222-222222222222.jsonl"
    m2.write_text(_meta_line("bbbbbbbb-2222-2222-2222-222222222222",
                             "2026-08-10T12:00:00Z") + "\n"
                  + _msg_line("m2-line-1") + "\n"
                  + _msg_line("m2-line-2") + "\n")
    return m1, m2


def _assert_ordered(env, name, m1_marks, m2_marks):
    texts = env.bus.message_texts(name)
    for mark in m1_marks + m2_marks:
        assert texts.count(mark) == 1, (texts, mark)
    last_m1 = max(texts.index(m) for m in m1_marks)
    first_m2 = min(texts.index(m) for m in m2_marks)
    assert last_m1 < first_m2, (
        f"every m1 line must publish BEFORE any m2 line; got {texts}"
    )


@pytest.mark.asyncio
async def test_9_rollover_with_undrained_tail_publishes_in_order(env):
    """L2.A test 9 (CalCedeTail in code): every m1 line publishes before
    any m2 line, exact counts, and the link advances only after m1 is at
    EOF."""
    name = "auto-hand9"
    m1, m2 = _write_chain_pair(env, name)

    mon = env.make_monitor()
    # The rollover CREATE arrives for m2 (dir armed late — not birth).
    mon.observe_rollout(name, m2, source="IN_CREATE", create_event=True)
    await _settle(mon)

    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(m2), "link must advance to the successor"
    assert row["file_offset"] == m2.stat().st_size
    _assert_ordered(env, name, ["m1-line-1", "m1-line-2"],
                    ["m2-line-1", "m2-line-2"])
    # entry_count accumulates across the chain: 3 raw lines per file.
    assert row["entry_count"] == 6
    # The superseded track is closed and sealed.
    t1 = mon._tracks[(name, str(m1))]
    assert t1.state == "CLOSED"
    assert t1.checked_size == m1.stat().st_size


@pytest.mark.asyncio
async def test_10_restart_between_create_and_link_converges(env):
    """L2.A test 10 (auto-ok297 / N3): a restart between the rollover's
    CREATE and its link leaves a LINKED row — the unrestricted tick must
    still observe the successor and converge, predecessors first."""
    name = "auto-hand10"
    m1, m2 = _write_chain_pair(env, name)

    # Fresh monitor = post-restart: no tracks, no kernel queue.
    mon = env.make_monitor()
    await mon.reconciliation_tick()
    await _settle(mon, rounds=10)

    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(m2), (
        "the tick must not be gated on jsonl_path IS NULL (N3)"
    )
    assert row["file_offset"] == m2.stat().st_size
    _assert_ordered(env, name, ["m1-line-1", "m1-line-2"],
                    ["m2-line-1", "m2-line-2"])


@pytest.mark.asyncio
async def test_11_late_flush_residual_is_logged_not_fought(env, caplog):
    """L2.A test 11: a write to the superseded file after the advance is
    the documented residual — no crash, no duplicate, logged with the
    file's checked level. Deliberately NO assertion that the late byte
    arrives (the impossibility theorem)."""
    name = "auto-hand11"
    m1, m2 = _write_chain_pair(env, name)
    mon = env.make_monitor()
    mon.observe_rollout(name, m2, source="IN_CREATE", create_event=True)
    await _settle(mon)
    assert _db_row(env.db_path, name)["jsonl_path"] == str(m2)

    before = list(env.bus.message_texts(name))
    with open(m1, "a") as fh:
        fh.write(_msg_line("late-flush-line") + "\n")
    import logging
    with caplog.at_level(logging.WARNING):
        await mon.reconciliation_tick()
        await _settle(mon)

    texts = env.bus.message_texts(name)
    assert texts.count("m1-line-1") == 1 and texts.count("m1-line-2") == 1, (
        "no duplicate of already-published predecessor content"
    )
    assert "late-flush residual" in caplog.text
    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(m2), "the link must not roll backwards"
    assert len(texts) >= len(before)   # no crash; monitor still live


@pytest.mark.asyncio
async def test_12_crash_between_publish_and_persist_bounded_duplicate(env):
    """L2.A test 12 (BoundedDuplicates): a crash between publish and
    persist re-delivers AT MOST the one in-flight window on retry; nothing
    is lost; the offset converges."""
    name = "auto-crash12"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    m1 = sdir / "rollout-2026-08-10T11-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    m1.write_text(_meta_line("aaaaaaaa-1111-1111-1111-111111111111") + "\n"
                  + _msg_line("crash-window-line") + "\n")
    env.db.update_jsonl_link(name, session_uuid=m1.stem, jsonl_path=str(m1))

    mon = env.make_monitor()
    row = env.db.get_session(name)
    # Simulate the crash: the worker publishes its window, then dies
    # before persisting the offset.
    window = mon._read_tail_window(dict(row))
    assert window is not None
    await mon._publish_tail_window(name, dict(row), window)
    # (no persist — crashed)
    assert _db_row(env.db_path, name)["file_offset"] == 0

    # Recovery: the next drain re-reads from the persisted offset.
    mon.request_drain(name)
    await _settle(mon)

    row = _db_row(env.db_path, name)
    assert row["file_offset"] == m1.stat().st_size, "offset must converge"
    texts = env.bus.message_texts(name)
    # The accepted residual: the in-flight window published twice, no more.
    assert texts.count("crash-window-line") == 2
    # entry_count only counts the persisted delivery (no double ack).
    assert row["entry_count"] == 2


@pytest.mark.asyncio
async def test_12b_handover_published_file_links_at_published_level(env):
    """L2.A test 12b(b): a trusted/late link of a file the handover already
    published must start reading at the published level, not byte 0."""
    name = "auto-hand12b"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    m2 = sdir / "rollout-2026-08-10T12-00-00-bbbbbbbb-2222-2222-2222-222222222222.jsonl"
    m2.write_text(_meta_line("bbbbbbbb-2222-2222-2222-222222222222",
                             "2026-08-10T12:00:00Z") + "\n"
                  + _msg_line("12b-line-1") + "\n")

    mon = env.make_monitor()
    # The handover publishes m2 while it is never-linked.
    progressed = await mon._publish_unlinked_segment(name, str(m2))
    assert progressed
    track = mon._tracks[(name, str(m2))]
    assert track.published_up_to == m2.stat().st_size
    assert env.bus.message_texts(name).count("12b-line-1") == 1

    # Its link event then arrives (late observation → promote).
    mon.observe_rollout(name, m2, source="IN_CREATE", create_event=True)
    await mon.reconciliation_tick()   # CAS-retry resolves through the recheck
    await _settle(mon)

    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(m2)
    assert row["file_offset"] == m2.stat().st_size, (
        "linking must never rewind publication (forbidden dup route 2)"
    )
    assert env.bus.message_texts(name).count("12b-line-1") == 1, (
        "no failure-free duplicate from the link re-reading byte 0"
    )


@pytest.mark.asyncio
async def test_13_deferred_handover_racing_direct_promotion(env):
    """L2.A test 13 (N5's counterexample): a deferred handover whose target
    the row ALREADY links must re-attach — the file streams, no CAS churn,
    no permanent CHARACTERIZING."""
    name = "auto-hand13"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    m2 = sdir / "rollout-2026-08-10T12-00-00-bbbbbbbb-2222-2222-2222-222222222222.jsonl"
    m2.write_text(_meta_line("bbbbbbbb-2222-2222-2222-222222222222",
                             "2026-08-10T12:00:00Z") + "\n"
                  + _msg_line("13-line-1") + "\n")
    # A direct promotion already linked m2 (offset persisted mid-file).
    env.db.update_jsonl_link(name, session_uuid=m2.stem, jsonl_path=str(m2))

    mon = env.make_monitor()
    gate = mon._gate(name)
    # The stale deferred handover from before the direct promotion:
    gate.pending_link = str(m2)
    gate.pending_expected = str(sdir / "rollout-2026-08-10T11-00-00-old.jsonl")
    track = mon._get_track(name, str(m2))
    track.state = "CHARACTERIZING"

    mon.request_drain(name)
    await _settle(mon)

    assert gate.pending_link is None
    assert track.state == "STREAMING", (
        "N5: the linked file's own track must never churn the CAS"
    )
    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(m2)
    assert row["file_offset"] == m2.stat().st_size


# ── Drain-gate discipline (Rule 5 / A5 / T87 / B7) ─────────────────────


def _link_single(env, name, marker="gate-line-1"):
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    m1 = sdir / "rollout-2026-08-10T11-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    m1.write_text(_meta_line("aaaaaaaa-1111-1111-1111-111111111111") + "\n"
                  + _msg_line(marker) + "\n")
    env.db.update_jsonl_link(name, session_uuid=m1.stem, jsonl_path=str(m1))
    return m1


@pytest.mark.asyncio
async def test_contended_request_transfers_and_is_never_skipped(env):
    """T87: a request against a busy gate transfers through the dirty flag
    — the skipped signal may be the only one those bytes get."""
    name = "auto-t87"
    # Monitor first: its init scan must not pre-drain the file this test
    # wants to arrive while the gate is (simulated) busy.
    mon = env.make_monitor()
    m1 = _link_single(env, name, "t87-line")
    gate = mon._gate(name)
    gate.busy = True   # a (simulated) owner holds the gate

    mon.request_drain(name)
    assert gate.dirty is True
    assert gate.needs_drain is False, "contended request must not double-pump"

    # The owner finishes: the release path must carry the transferred
    # signal to a continuation, and the bytes must land.
    mon._release_drain_gate(name)
    await _settle(mon)
    row = _db_row(env.db_path, name)
    assert row["file_offset"] == m1.stat().st_size
    assert env.bus.message_texts(name).count("t87-line") == 1


@pytest.mark.asyncio
async def test_cancelled_drain_releases_only_when_worker_stops(env, monkeypatch):
    """A5/B4: cancelling the awaiter does not stop the executor worker.
    The owner awaits a SHIELDED inner future, so the gate releases only
    when the worker thread actually finishes — no second read may begin
    before that release, and responsibility is retained.

    The owner task is captured BY CODE OBJECT (the review found the old
    __qualname__ lookup can match nothing and cancel zero tasks, making
    the assertion vacuous — by then the continuation owner had re-claimed
    ``busy``)."""
    import threading

    name = "auto-a5"
    m1 = _link_single(env, name, "a5-line")
    mon = env.make_monitor()
    await _settle(mon)   # let any init-scan drain finish first
    env.bus.broadcasts.clear()
    env.db.update_tail_state(name, file_offset=0)   # force a re-drain window

    worker_inside = threading.Event()
    worker_done = threading.Event()
    reads = {"n": 0, "max": 0}
    lock = threading.Lock()
    real_read = mon._read_tail_window
    slow = {"t": 0.4}

    def slow_read(row, parse_ctx=None):
        with lock:
            reads["n"] += 1
            reads["max"] = max(reads["max"], reads["n"])
        worker_inside.set()
        try:
            time.sleep(slow["t"])
            return real_read(row, parse_ctx)
        finally:
            with lock:
                reads["n"] -= 1
            worker_done.set()

    monkeypatch.setattr(mon, "_read_tail_window", slow_read)
    mon.request_drain(name)
    gate = mon._gate(name)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if worker_inside.is_set():
            break
    assert worker_inside.is_set(), "drain worker never started"

    # Capture the REAL owner task by code object and cancel it mid-read.
    drain_code = mon._drain_as_owner.__func__.__code__
    owner = next(
        (t for t in asyncio.all_tasks()
         if getattr(t.get_coro(), "cr_code", None) is drain_code),
        None,
    )
    assert owner is not None, "owner task not found by code object"
    assert owner is gate.owner_task, "gate must track its owner task"
    owner.cancel()
    slow["t"] = 0.0   # the continuation's reads run fast
    await asyncio.sleep(0.1)

    # The worker thread is still inside its read: the gate must be HELD
    # and no second read may have begun.
    assert not worker_done.is_set(), "test invalid — worker already done"
    assert gate.busy, "gate released while the worker was still running"
    assert reads["max"] == 1, (
        f"a second read began before worker release (max={reads['max']})"
    )

    await asyncio.sleep(0.5)    # worker finishes → release → continuation
    await _settle(mon, rounds=20)

    assert reads["max"] == 1, "reads overlapped across the release"
    row = _db_row(env.db_path, name)
    assert row["file_offset"] == m1.stat().st_size, "responsibility retained"
    assert env.bus.message_texts(name).count("a5-line") == 1, (
        "the cancelled worker must not have produced a second tail"
    )


@pytest.mark.asyncio
async def test_composer_ready_survives_concurrent_poller_write(env):
    """B7/ComposerSticky: harness_state merges INSIDE the persist UPDATE —
    a poller write landing between the drain's read and its persist is
    never clobbered."""
    name = "auto-b7"
    m1 = _link_single(env, name, "b7-line")
    mon = env.make_monitor()
    row = env.db.get_session(name)
    window = mon._read_tail_window(dict(row))
    assert window is not None
    # This drain pass changed some harness key of its own...
    window["harness_state_patch"] = json.dumps({"drain_key": 1})
    await mon._publish_tail_window(name, dict(row), window)
    # ...and between read and persist, the screen poller writes
    # composer_ready (the exact RMW-across-await hazard).
    env.db.update_tail_state(name, harness_state='{"composer_ready": true}')
    assert mon._persist_tail_window(name, dict(row), window)

    hs = json.loads(_db_row(env.db_path, name)["harness_state"])
    assert hs.get("composer_ready") is True, "poller write must survive"
    assert hs.get("drain_key") == 1, "drain's own delta must also land"


# ── Consolidated-review pins (codex + deep review, 2026-08-10) ─────────


@pytest.mark.asyncio
async def test_b1_host_row_never_adopts_shared_dir_file(env):
    """B1: ambient observation of a HOST row's shared directory has no
    ownership evidence — an unlinked host row must NOT first-resolve onto
    a sibling file, and no track may even be created for it (per-tick
    churn over years of project history)."""
    shared = env.tmp_path / "projects" / "-workspace-repo"
    shared.mkdir(parents=True)
    foreign = shared / "11111111-2222-3333-4444-555555555555.jsonl"
    foreign.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "someone else's session"}]},
        "uuid": "m1", "parentUuid": None,
    }) + "\n")
    env.db.insert_session(
        tmux_name="host-victim", session_type="host", project="-workspace-repo",
        harness="claude", resolution_dir=str(shared),
    )
    mon = env.make_monitor()
    await mon.reconciliation_tick()
    await _settle(mon)

    row = _db_row(env.db_path, "host-victim")
    assert row["jsonl_path"] is None, "host row adopted a foreign file"
    assert ("host-victim", str(foreign)) not in mon._tracks, (
        "no track may be created for a non-owned shared-dir file"
    )
    assert env.bus.message_texts("host-victim") == []


@pytest.mark.asyncio
async def test_b2_inode_watch_shared_by_two_tracks_survives_single_release(env):
    """B2/D3: two tracks on one inode subscribe to ONE watch entry;
    releasing one subscriber never drops the other's watch, and the last
    release removes the kernel watch and the dispatch mapping (so a
    recycled wd can never dispatch into a stale track)."""
    shared = env.tmp_path / "shared" / "sessions"
    shared.mkdir(parents=True)
    empty = shared / "rollout-2026-08-10T12-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    empty.touch()
    for name in ("auto-two-a", "auto-two-b"):
        env.db.insert_session(
            tmux_name=name, session_type="container", project="autonomy",
            harness="codex", resolution_dir=str(shared),
        )
    mon = env.make_monitor()
    mon.observe_rollout("auto-two-a", empty, source="reconciliation")
    mon.observe_rollout("auto-two-b", empty, source="reconciliation")

    ta = mon._tracks[("auto-two-a", str(empty))]
    tb = mon._tracks[("auto-two-b", str(empty))]
    assert ta.wd is not None and ta.wd == tb.wd   # one inode, one wd
    inode_key = mon._wd_to_inode[ta.wd]
    entry = mon._inode_watches[inode_key]
    assert entry["subscribers"] == {
        ("track", "auto-two-a", str(empty)),
        ("track", "auto-two-b", str(empty)),
    }

    # Releasing b keeps a's subscription, mapping, and kernel watch.
    mon._release_track_watch(tb)
    assert tb.wd is None
    assert ta.wd is not None
    assert mon._wd_to_inode.get(ta.wd) == inode_key
    assert entry["subscribers"] == {("track", "auto-two-a", str(empty))}

    # Last release removes everything — a stale queued wd maps nowhere.
    old_wd = ta.wd
    mon._release_track_watch(ta)
    assert old_wd not in mon._wd_to_inode
    assert inode_key not in mon._inode_watches


@pytest.mark.asyncio
async def test_b3_three_rollovers_publish_each_file_exactly_once(env):
    """B3 (failure-free dup route #3): m1 → m2 → m3 with zero failures —
    every line exactly once, entry_count exact. The linked file's
    persisted offset folds into its track (and tombstone) at close, so
    the m3 handover never re-reads m1 from byte 0."""
    name = "auto-chain-pin"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    files = []
    for hour, uid in (("11", "aaaaaaaa-1111-1111-1111-111111111111"),
                      ("12", "bbbbbbbb-2222-2222-2222-222222222222"),
                      ("13", "cccccccc-3333-3333-3333-333333333333")):
        f = sdir / f"rollout-2026-08-10T{hour}-00-00-{uid}.jsonl"
        files.append((f, f"m{len(files) + 1}"))
    mon = env.make_monitor()

    for i, (f, mark) in enumerate(files):
        uid = f.name.split("-", 4)[-1].removesuffix(".jsonl")
        f.write_text(
            _meta_line(uid, f"2026-08-10T{11 + i}:00:00Z") + "\n"
            + _msg_line(f"{mark}-line-1") + "\n"
        )
        if i == 0:
            mon.observe_rollout(name, f, source="reconciliation")
        else:
            mon.observe_rollout(name, f, source="IN_CREATE", create_event=True)
        await _settle(mon)

    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(files[2][0])
    texts = env.bus.message_texts(name)
    for _, mark in files:
        assert texts.count(f"{mark}-line-1") == 1, (mark, texts)
    assert row["entry_count"] == 6, f"entry_count={row['entry_count']}"
    # The folded level rides the tombstone (survives a reopen).
    t1 = mon._tracks[(name, str(files[0][0]))]
    assert t1.published_up_to == files[0][0].stat().st_size
    assert t1.tombstone is not None and t1.tombstone[4] == t1.published_up_to


@pytest.mark.asyncio
async def test_b4_purity_read_workers_never_write_or_broadcast(env):
    """B4 purity pin: the executor read workers are the ONLY code that a
    cancelled awaiter cannot stop — they must be pure reads. Any DB write,
    broadcast, or monitor-state mutation here reopens the double-tail."""
    name = "auto-pure"
    m1 = _link_single(env, name, "purity-line")
    mon = env.make_monitor()
    await _settle(mon)   # let init-scan work finish
    # Re-open a read window (the init drain consumed the file to EOF).
    env.db.update_tail_state(name, file_offset=0)
    env.bus.broadcasts.clear()
    row_before = _db_row(env.db_path, name)
    tracks_before = {k: (t.state, t.published_up_to, t.wd)
                     for k, t in mon._tracks.items()}

    row = env.db.get_session(name)
    w1 = mon._read_tail_window(dict(row))
    w2 = mon._read_segment_window(dict(row), str(m1), 0, None)
    assert w1 is not None and w2 is not None

    assert _db_row(env.db_path, name) == row_before, "worker wrote the DB"
    assert env.bus.broadcasts == [], "worker broadcast"
    assert {k: (t.state, t.published_up_to, t.wd)
            for k, t in mon._tracks.items()} == tracks_before, (
        "worker mutated monitor state"
    )


@pytest.mark.asyncio
async def test_publish_unlinked_segment_survives_mid_read_deregister(env):
    """auto-16g9t round-1 addendum item 3: a deregister landing during the
    shielded segment read pops the session's tail state — the post-await
    guard must re-create it rather than KeyError into the crash path
    (degrade-not-lockup). The guard predates commit A's pre-executor
    creation and both are load-bearing."""
    name = "auto-midderegs"
    m1 = _link_single(env, name, "midderegs-line")
    mon = env.make_monitor()
    await _settle(mon)

    real_read = mon._read_segment_window

    def popping_read(row, path, start_offset, expect_generation, parse_ctx=None):
        # Simulate the deregister racing the shielded read.
        mon._tail_states.pop(name, None)
        return real_read(row, path, 0, None, parse_ctx)

    mon._read_segment_window = popping_read
    progressed = await mon._publish_unlinked_segment(name, str(m1))
    assert progressed, "publication must proceed after a mid-read deregister"
    assert name in mon._tail_states, "tail state re-created by the guard"


@pytest.mark.asyncio
async def test_b5_teardown_purges_tracks_gates_and_watches(env):
    """B5: session death purges FileTracks, inode/wd mappings, dir epochs,
    and retires the gate; post-death MODIFY dispatches nothing and the
    kernel watches are actually gone."""
    name = "auto-dead"
    m1 = _link_single(env, name, "teardown-line")
    # A second, characterizing file so track-watch teardown is exercised.
    pending = m1.parent / "rollout-2026-08-10T12-00-00-eeeeeeee-5555-5555-5555-555555555555.jsonl"
    pending.touch()
    mon = env.make_monitor()
    mon.observe_rollout(name, pending, source="reconciliation")
    await _settle(mon)
    assert any(k[0] == name for k in mon._tracks)
    assert mon._session_gates.get(name) is not None

    mon._remove_watches(name)

    assert not any(k[0] == name for k in mon._tracks), "tracks not purged"
    assert not any(k[0] == name for k in mon._dir_epochs), "epochs not purged"
    gate = mon._session_gates.get(name)
    assert gate is None or gate.retired, "gate not retired"
    assert not any(
        sub[1] == name
        for e in mon._inode_watches.values()
        for sub in e["subscribers"]
    ), "inode/file subscription not removed"

    # Post-death writes produce no dispatchable events: drain the queue
    # (IN_IGNORED from the removals), then write and check again.
    mon._inotify.read(timeout=100)
    with open(m1, "a") as fh:
        fh.write(_msg_line("after-death") + "\n")
    events = mon._inotify.read(timeout=200)
    for ev in events:
        inode_key = mon._wd_to_inode.get(ev.wd)
        subs = (
            mon._inode_watches.get(inode_key, {}).get("subscribers", set())
            if inode_key else set()
        )
        assert not any(s[1] == name for s in subs)
    # A stray request against the retired gate is refused.
    if gate is not None:
        mon.request_drain(name)
        assert not gate.busy and not gate.needs_drain


@pytest.mark.asyncio
async def test_b6_quiet_host_burst_becomes_visible_without_further_write(env):
    """B6: the host launch watcher activates through the unified machine —
    a burst already on disk persists offset/count/last_message with NO
    further write, the generation is stamped atomically with the link,
    and no linked-but-zero registry broadcast precedes the drain."""
    name = "host-quiet"
    projects = env.tmp_path / "projects" / "-workspace-repo"
    projects.mkdir(parents=True)
    env.db.insert_session(
        tmux_name=name, session_type="host", project="-workspace-repo",
        harness="claude", resolution_dir=str(projects),
    )
    mon = env.make_monitor()
    from tools.dashboard import session_harness as sh
    watcher = asyncio.create_task(
        sh._watch_for_claude_host_jsonl(mon, projects, name, timeout=5.0),
    )
    await asyncio.sleep(0.6)   # watcher snapshots the (empty) dir
    burst = projects / "99999999-8888-7777-6666-555555555555.jsonl"
    burst.write_text(
        json.dumps({"type": "user", "uuid": "u1", "parentUuid": None,
                    "message": {"role": "user", "content": "host question"}}) + "\n"
        + json.dumps({"type": "assistant", "uuid": "a1",
                      "message": {"role": "assistant", "content": [
                          {"type": "text", "text": "host burst answer text"}]}}) + "\n"
    )
    await watcher
    await _settle(mon, rounds=20)

    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(burst)
    assert row["jsonl_generation"], "generation must be stamped with the link"
    assert row["file_offset"] == burst.stat().st_size, (
        "the burst must persist with no further write"
    )
    assert row["entry_count"] == 2
    assert "host burst answer" in (row["last_message"] or "")
    # No linked-but-zero registry broadcast before the burst published.
    # (Round 2: the original loop keyed rows by 'tmux_name'/'jsonl_path'
    # — keys registry rows don't have — and was vacuous; both reviewers
    # found it. Rewritten against session_id/resolved/entry_count.)
    _assert_registry_publishes_after_drain(env.bus, name)


@pytest.mark.asyncio
async def test_b6_stale_truthy_generation_is_repaired_on_reattach(env):
    """B6: a persisted re-attach must REPAIR a stale truthy generation
    (inode changed while the path stayed), not only backfill a missing
    one — otherwise every read re-stats, mismatches, and rejects the new
    inode forever."""
    name = "auto-genrepair"
    m1 = _link_single(env, name, "repair-line")
    # Poison the generation with an inode that can never match.
    conn = sqlite3.connect(str(env.db_path))
    conn.execute(
        "UPDATE tmux_sessions SET jsonl_generation='999999:999999:1'"
        " WHERE tmux_name=?", (name,),
    )
    conn.commit()
    conn.close()

    mon = env.make_monitor()
    mon.observe_rollout(name, m1, source="startup_recovery")
    await _settle(mon)

    row = _db_row(env.db_path, name)
    st = m1.stat()
    assert row["jsonl_generation"].startswith(f"{st.st_dev}:{st.st_ino}:"), (
        f"stale generation not repaired: {row['jsonl_generation']}"
    )
    assert row["file_offset"] == st.st_size, (
        "reads must accept the repaired identity and drain to EOF"
    )
    assert env.bus.message_texts(name).count("repair-line") == 1


# ── Round-2 review pins (R1-R3, S2 — 2026-08-10) ───────────────────────


@pytest.mark.asyncio
async def test_r1_larger_replacement_resets_cursor_no_loss(env):
    """R1: ANY (dev,ino) mismatch on re-attach resets the cursor to 0
    atomically with the generation repair. A same-or-larger replacement
    file must republish IN FULL — a kept stale cursor reads mid-line and
    silently loses the line spanning it (loss never; the replacement is a
    failure event and its re-read duplicates are the accepted residual)."""
    name = "auto-r1pin"
    sdir = env.tmp_path / name / "sessions"
    env.make_session(name, sdir)
    path = sdir / "rollout-2026-08-10T11-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    path.write_text(_msg_line("old-line-1") + "\n")
    old_offset = path.stat().st_size
    env.db.update_jsonl_link(
        name, session_uuid=path.stem, jsonl_path=str(path),
        generation="424242:424242:1",   # stale — describes the replaced inode
        file_offset=old_offset,
    )
    # Larger replacement whose FIRST line spans the stale cursor.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        _msg_line("replacement-first-line " + "x" * 120) + "\n"
        + _msg_line("replacement-second-line") + "\n"
    )
    tmp.rename(path)
    assert path.stat().st_size >= old_offset

    mon = env.make_monitor()
    mon.observe_rollout(name, path, source="startup_recovery")
    await _settle(mon, rounds=20)

    row = _db_row(env.db_path, name)
    st = path.stat()
    assert row["jsonl_generation"].startswith(f"{st.st_dev}:{st.st_ino}:")
    texts = env.bus.message_texts(name)
    assert any(t.startswith("replacement-first-line") for t in texts), (
        f"first line lost: {texts}"
    )
    assert texts.count("replacement-second-line") == 1
    assert row["file_offset"] == st.st_size
    assert row["entry_count"] == 2
    # Secondary (observed in the repro): a mid-line read also inflates
    # parse_errors — a full re-read must not.
    ts = mon._tail_states.get(name)
    assert ts is not None and ts.parse_errors_count == 0


@pytest.mark.asyncio
async def test_r2_two_streaming_sessions_share_inode_watch(env):
    """R2: two STREAMING sessions on one inode subscribe to ONE entry;
    MODIFY fans out to BOTH; removing one session's watches keeps the
    other's subscription and the kernel watch."""
    shared = env.tmp_path / "projects" / "-workspace-repo"
    shared.mkdir(parents=True)
    f = shared / "11111111-2222-3333-4444-555555555555.jsonl"
    f.write_text(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "shared transcript"}]},
    }) + "\n")
    mon = env.make_monitor()
    for name in ("host-r2p-a", "host-r2p-b"):
        env.db.insert_session(
            tmux_name=name, session_type="host", project="-workspace-repo",
            harness="claude", resolution_dir=str(shared),
        )
        env.db.update_jsonl_link(name, session_uuid=f.stem, jsonl_path=str(f))
        mon._add_file_watch(name, str(f))

    ts_a = mon._tail_states["host-r2p-a"]
    ts_b = mon._tail_states["host-r2p-b"]
    assert ts_a.watch_descriptor == ts_b.watch_descriptor
    wd = ts_a.watch_descriptor
    inode_key = mon._wd_to_inode[wd]
    subs = mon._inode_watches[inode_key]["subscribers"]
    assert ("session", "host-r2p-a") in subs
    assert ("session", "host-r2p-b") in subs

    # MODIFY fan-out reaches BOTH sessions.
    assert mon._dispatch_modify_wd(wd) == {"host-r2p-a", "host-r2p-b"}

    # Removing b keeps a's subscription and the kernel/dispatch mapping.
    mon._remove_watches("host-r2p-b")
    assert ("session", "host-r2p-a") in mon._inode_watches[inode_key]["subscribers"]
    assert ("session", "host-r2p-b") not in mon._inode_watches[inode_key]["subscribers"]
    assert mon._wd_to_inode.get(wd) == inode_key
    assert mon._dispatch_modify_wd(wd) == {"host-r2p-a"}


@pytest.mark.asyncio
async def test_r2_mixed_streaming_and_track_fan_out(env):
    """R2: a streaming session and a characterizing track on one inode —
    MODIFY dispatch reaches BOTH (no scalar-map shadowing), and the
    track's release never drops the streaming session's kernel watch."""
    shared = env.tmp_path / "shared" / "sessions"
    shared.mkdir(parents=True)
    f = shared / "rollout-2026-08-10T12-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    f.touch()
    mon = env.make_monitor()
    env.db.insert_session(
        tmux_name="auto-r2p-stream", session_type="container",
        project="autonomy", harness="codex", resolution_dir=str(shared),
    )
    env.db.update_jsonl_link(
        "auto-r2p-stream", session_uuid=f.stem, jsonl_path=str(f),
    )
    mon._add_file_watch("auto-r2p-stream", str(f))
    env.db.insert_session(
        tmux_name="auto-r2p-char", session_type="container",
        project="autonomy", harness="codex", resolution_dir=str(shared),
    )
    mon.observe_rollout("auto-r2p-char", f, source="reconciliation")
    track = mon._tracks[("auto-r2p-char", str(f))]
    assert track.state == "CHARACTERIZING"
    wd = mon._tail_states["auto-r2p-stream"].watch_descriptor
    assert track.wd == wd, "one inode → one shared watch entry"

    # Write the fork header; MODIFY dispatch must BOTH return the
    # streaming session AND reclassify the track (no shadowing).
    f.write_text(_fork_meta_line("aaaaaaaa-1111-1111-1111-111111111111") + "\n")
    modified = mon._dispatch_modify_wd(wd)
    assert modified == {"auto-r2p-stream"}
    assert track.state == "IGNORED", "track recheck was shadowed"

    # The track's release (classification resolved → IGNORED already
    # released it) must have left the streaming watch intact.
    inode_key = mon._wd_to_inode.get(wd)
    assert inode_key is not None, "kernel watch dropped with the track"
    assert ("session", "auto-r2p-stream") in mon._inode_watches[inode_key]["subscribers"]
    # And the file did NOT get IN_IGNORED'd: no event tears it down.
    mon._dispatch_ignored_wd(wd)   # stale IGNORED → revalidates, survives
    assert mon._wd_to_inode.get(wd) == inode_key


@pytest.mark.asyncio
async def test_r2_wd_reuse_after_ignored(env, monkeypatch):
    """R2 (dedicated wd-reuse pin, flagged missing since round 1): after a
    watch is torn down, a QUEUED stale event with the old wd dispatches
    nothing; a stale IGNORED arriving while a live entry owns the number
    is revalidated by re-stat and does NOT tear the live entry down; a
    genuine IGNORED (file gone) does."""
    sdir = env.tmp_path / "auto-reuse" / "sessions"
    env.make_session("auto-reuse", sdir)
    fa = sdir / "rollout-2026-08-10T12-00-00-aaaaaaaa-1111-1111-1111-111111111111.jsonl"
    fa.touch()
    mon = env.make_monitor()
    mon.observe_rollout("auto-reuse", fa, source="reconciliation")
    track_a = mon._tracks[("auto-reuse", str(fa))]
    wd_a = track_a.wd
    assert wd_a is not None

    calls = {"n": 0}
    real_classify = mon._classify_and_step

    def counting_classify(track, **kw):
        calls["n"] += 1
        return real_classify(track, **kw)

    monkeypatch.setattr(mon, "_classify_and_step", counting_classify)

    # Tear down (quarantine-style release), then a stale queued MODIFY
    # with the old wd arrives: dispatches nothing.
    mon._release_track_watch(track_a)
    assert mon._dispatch_modify_wd(wd_a) == set()
    assert calls["n"] == 0

    # A new watch that recycles the number: simulate by arming a live
    # entry, then delivering a STALE IGNORED for its wd while its file
    # still exists — re-stat revalidation must keep it.
    fb = sdir / "rollout-2026-08-10T13-00-00-bbbbbbbb-2222-2222-2222-222222222222.jsonl"
    fb.touch()
    mon.observe_rollout("auto-reuse", fb, source="reconciliation")
    track_b = mon._tracks[("auto-reuse", str(fb))]
    wd_b = track_b.wd
    inode_b = mon._wd_to_inode[wd_b]
    mon._dispatch_ignored_wd(wd_b)   # stale (fb still exists)
    assert mon._wd_to_inode.get(wd_b) == inode_b, (
        "stale IGNORED tore down a live entry"
    )
    assert track_b.wd == wd_b

    # Genuine IGNORED: the file is gone → entry detaches, track wd
    # cleared, epoch bumped.
    epoch_before = track_b.wd_epoch
    fb.unlink()
    mon._dispatch_ignored_wd(wd_b)
    assert wd_b not in mon._wd_to_inode
    assert track_b.wd is None
    assert track_b.wd_epoch > epoch_before


@pytest.mark.asyncio
async def test_r3_link_and_enrich_atomic_by_default(env, monkeypatch):
    """R3: link_and_enrich derives generation (and resets the cursor when
    the path moves) BY DEFAULT — no caller can create a path/generation
    disagreement window."""
    import subprocess as _sp
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
    )
    name = "host-r3pin"
    shared = env.tmp_path / "projects" / "-workspace-repo"
    shared.mkdir(parents=True)
    env.db.insert_session(
        tmux_name=name, session_type="host", project="-workspace-repo",
        harness="claude", resolution_dir=str(shared),
    )
    old = shared / "00000000-old.jsonl"
    old.write_text("{}\n")
    env.db.update_jsonl_link(
        name, session_uuid=old.stem, jsonl_path=str(old),
        generation="7:7:1", file_offset=2,
    )
    new = shared / "11111111-new.jsonl"
    new.write_text(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")

    # The exact bare server call shape — no generation, no offset.
    env.db.link_and_enrich(
        name, session_uuid=new.stem, jsonl_path=str(new),
        project="-workspace-repo",
    )
    row = _db_row(env.db_path, name)
    st = new.stat()
    assert row["jsonl_path"] == str(new)
    assert row["jsonl_generation"].startswith(f"{st.st_dev}:{st.st_ino}:"), (
        f"generation not derived: {row['jsonl_generation']!r}"
    )
    assert row["file_offset"] == 0, "cursor must reset when the path moves"


def _assert_registry_publishes_after_drain(bus, name) -> None:
    """The invariant-9 shape, both directions (round-2 polish: the
    absence-only form is vacuous when no registry is emitted at all):

    - BEFORE the first session:messages broadcast, no registry row for
      the session shows resolved=True with zero entry_count (the durable
      linked-but-zero signature);
    - AFTER it, EXACTLY ONE registry broadcast carries the session
      resolved with a nonzero entry_count (the post-drain publish).

    Registry rows key on session_id and expose resolved/entry_count."""
    first_msgs = next(
        (i for i, (t, p) in enumerate(bus.broadcasts)
         if t == "session:messages" and p.get("session_id") == name),
        None,
    )
    assert first_msgs is not None, "no session:messages broadcast at all"
    for topic, payload in bus.broadcasts[:first_msgs]:
        if topic != "session:registry":
            continue
        entry = next(
            (r for r in payload if r.get("session_id") == name), None,
        )
        assert not (
            entry is not None
            and entry.get("resolved")
            and not entry.get("entry_count")
        ), "durable linked-but-zero registry broadcast before the drain"
    post_drain = [
        entry
        for topic, payload in bus.broadcasts[first_msgs:]
        if topic == "session:registry"
        for entry in [
            next((r for r in payload if r.get("session_id") == name), None)
        ]
        if entry is not None
        and entry.get("resolved")
        and (entry.get("entry_count") or 0) > 0
    ]
    assert len(post_drain) == 1, (
        f"expected exactly one post-drain resolved registry row, "
        f"got {len(post_drain)}"
    )


@pytest.mark.asyncio
async def test_r3_active_server_host_watcher_quiet_burst(env, monkeypatch):
    """R3 (the discriminating host-activation pin): the ACTIVE
    fingerprint-matching server._watch_for_host_session_jsonl links with a
    derived generation, drains the burst already on disk with NO further
    write, and never broadcasts a resolved-with-zero-entries registry row."""
    from tools.dashboard import server as server_mod

    name = "host-server-quiet"
    projects = env.tmp_path / "projects" / "-workspace-repo"
    projects.mkdir(parents=True)
    env.db.insert_session(
        tmux_name=name, session_type="host", project="-workspace-repo",
        harness="claude", resolution_dir=str(projects),
    )
    mon = env.make_monitor()
    monkeypatch.setattr(server_mod, "session_monitor", mon)
    import subprocess as _sp
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
    )

    watcher = asyncio.create_task(
        server_mod._watch_for_host_session_jsonl(projects, name, timeout=5.0),
    )
    await asyncio.sleep(0.6)
    burst = projects / "99999999-8888-7777-6666-555555555555.jsonl"
    burst.write_text(
        json.dumps({"type": "user", "uuid": "u1", "parentUuid": None,
                    "message": {"role": "user",
                                "content": f"orientation for {name}"}}) + "\n"
        + json.dumps({"type": "assistant", "uuid": "a1",
                      "message": {"role": "assistant", "content": [
                          {"type": "text",
                           "text": "server watcher burst answer"}]}}) + "\n"
    )
    await watcher
    await _settle(mon, rounds=20)

    row = _db_row(env.db_path, name)
    st = burst.stat()
    assert row["jsonl_path"] == str(burst)
    assert row["jsonl_generation"].startswith(f"{st.st_dev}:{st.st_ino}:")
    assert row["file_offset"] == st.st_size, (
        "burst must persist with no further write"
    )
    assert row["entry_count"] == 2
    assert "server watcher burst" in (row["last_message"] or "")
    _assert_registry_publishes_after_drain(env.bus, name)


@pytest.mark.asyncio
async def test_r3_confirm_link_drains_and_stamps_generation(env, monkeypatch):
    """R3: the confirm-link handshake path links atomically (generation
    derived inside link_and_enrich), drains the transcript's existing
    bytes, and publishes the registry only after the drain."""
    from tools.dashboard import server as server_mod

    name = "host-confirm"
    projects_root = env.tmp_path / "home" / ".claude" / "projects"
    project_dir = projects_root / "-workspace-repo"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: env.tmp_path / "home"))
    env.db.insert_session(
        tmux_name=name, session_type="host", project="-workspace-repo",
        harness="claude", resolution_dir=str(project_dir),
    )
    mon = env.make_monitor()
    monkeypatch.setattr(server_mod, "session_monitor", mon)
    import subprocess as _sp
    monkeypatch.setattr(
        _sp, "run",
        lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
    )

    # ASCII-only: json.dumps escapes non-ASCII in the stored line, and the
    # handler's substring match runs against the RAW line text.
    handshake = "[dashboard] confirming terminal link please reply with I SEE IT"
    jf = project_dir / "44444444-3333-2222-1111-000000000000.jsonl"
    jf.write_text(
        json.dumps({"type": "user", "uuid": "u1", "parentUuid": None,
                    "message": {"role": "user", "content": handshake}}) + "\n"
        + json.dumps({"type": "assistant", "uuid": "a1",
                      "message": {"role": "assistant", "content": [
                          {"type": "text", "text": "I SEE IT"}]}}) + "\n"
    )

    class _FakeRequest:
        async def json(self):
            return {"tmux_session": name, "handshake": handshake}

    resp = await server_mod.api_session_confirm_link(_FakeRequest())
    assert resp.status_code == 200
    await _settle(mon, rounds=20)

    row = _db_row(env.db_path, name)
    st = jf.stat()
    assert row["jsonl_path"] == str(jf)
    assert row["jsonl_generation"].startswith(f"{st.st_dev}:{st.st_ino}:")
    assert row["file_offset"] == st.st_size
    assert row["entry_count"] == 2
    _assert_registry_publishes_after_drain(env.bus, name)


@pytest.mark.asyncio
async def test_s2_stop_quiesces_drains_and_start_resumes(env, monkeypatch):
    """S2: at stop() return there are ZERO live drain owners and the gate
    is not busy; responsibility is RETAINED (needs_drain) and the next
    start() resumes it."""
    name = "auto-s2pin"
    m1 = _link_single(env, name, "s2pin-line")
    mon = env.make_monitor()
    monkeypatch.setattr(
        mon.__class__, "_check_tmux", staticmethod(lambda _n: True),
    )
    from tools.dashboard.session_harness import parse_claude_log_line
    await mon.start(event_bus=env.bus, entry_parser=parse_claude_log_line)
    await _settle(mon)

    real_read = mon._read_tail_window
    slow = {"t": 0.4}

    def slow_read(row, parse_ctx=None):
        time.sleep(slow["t"])
        return real_read(row, parse_ctx)

    monkeypatch.setattr(mon, "_read_tail_window", slow_read)
    env.db.update_tail_state(name, file_offset=0)
    env.bus.broadcasts.clear()
    mon.request_drain(name)
    await asyncio.sleep(0.1)   # owner inside its slow read

    await mon.stop()

    drain_code = mon._drain_as_owner.__func__.__code__
    live_owners = [
        t for t in asyncio.all_tasks()
        if getattr(t.get_coro(), "cr_code", None) is drain_code
        and not t.done()
    ]
    assert live_owners == [], "stop() must leave zero live drain owners"
    gate = mon._session_gates.get(name)
    assert gate is not None and not gate.busy
    assert gate.needs_drain, "responsibility must be RETAINED for restart"

    # The next start() resumes the retained work.
    slow["t"] = 0.0
    await mon.start(event_bus=env.bus, entry_parser=parse_claude_log_line)
    await _settle(mon, rounds=20)
    row = _db_row(env.db_path, name)
    assert row["file_offset"] == m1.stat().st_size
    assert env.bus.message_texts(name).count("s2pin-line") == 1
    await mon.stop()


# ── Lifecycle non-interference ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_columns_byte_identical_through_ingestion(env):
    """state/startup_state belong exclusively to STATE_AUTHORITY: they must
    be byte-identical through characterize → promote → handover → drain."""
    name = "auto-lifecycle"
    m1, m2 = _write_chain_pair(env, name)
    conn = sqlite3.connect(str(env.db_path))
    conn.execute(
        "UPDATE tmux_sessions SET state='ACTIVE', startup_state=NULL"
        " WHERE tmux_name=?", (name,),
    )
    conn.commit()
    conn.close()

    def lifecycle_bytes():
        row = _db_row(env.db_path, name)
        return (row["state"], row["startup_state"])

    baseline = lifecycle_bytes()
    mon = env.make_monitor()

    # characterize (empty extra file)
    extra = m2.parent / "rollout-2026-08-10T13-00-00-cccccccc-3333-3333-3333-333333333333.jsonl"
    extra.touch()
    mon.observe_rollout(name, extra, source="reconciliation")
    assert lifecycle_bytes() == baseline

    # promote + ordered handover + drain
    mon.observe_rollout(name, m2, source="IN_CREATE", create_event=True)
    await _settle(mon)
    assert lifecycle_bytes() == baseline

    # reconciliation + quarantine of the still-empty extra file
    track = mon._tracks[(name, str(extra))]
    track.characterize_deadline = time.time() - 1
    await mon.reconciliation_tick()
    await _settle(mon)
    assert lifecycle_bytes() == baseline


def _claude_queue_line(session_id: str, ts: str, text: str = "ok") -> str:
    """The first line the real stubs carried — a queued user message.

    Both 01:25 files opened with this, 12,478 bytes and 8 lines each. They
    were never empty, which is why N2 has nothing to say about them.
    """
    return json.dumps({
        "type": "queue-operation", "operation": "add",
        "sessionId": session_id, "timestamp": ts, "content": text,
    })


def _claude_line(text: str, role: str = "assistant",
                 ts: str = "2026-08-14T05:00:00.000Z") -> str:
    """One Claude-transcript line. Claude uses no session_meta header and
    names its files <uuid>.jsonl, with no timestamp or ordering in the name."""
    return json.dumps({
        "timestamp": ts,
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    })


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN BUG, reproduced. A created <uuid>.jsonl supersedes the linked "
        "transcript on sight: _classify_codex_rollout returns 'main' for any "
        "name not starting with 'rollout-' without reading or stat'ing the "
        "file, so the characterize state that enforces N2 is never entered for "
        "Claude. Cost: auto-0730-021228 froze at 01:25 on 2026-08-14 for ten "
        "hours. Needs create_event=True to fail at all.\n\n"
        "TWO FIXES TRIED AND REVERTED, both measured:\n"
        "1. parentUuid-non-null gate — wrong question. The stillborn stubs DO "
        "carry parentUuids (4 each); they just point at their own entries. "
        "Broke test_cross_boundary + test_sse_delivery.\n"
        "2. parentUuid-chain gate (do the candidate's early refs resolve INTO "
        "the linked file?) — right question, verified 13/13 on this host's real "
        "corpus: 2 genuine rollovers hit, 10 stillborn stubs and 1 unrelated "
        "session miss. But it regressed rollover TIMING: a real successor is "
        "created EMPTY and written a moment later, and observe_rollout has no "
        "per-modify hook for an unlinked file — it is re-called only from scan/"
        "registry paths. So adoption slipped from immediate to scan-delayed.\n\n"
        "The chain test is the right discriminator. It needs the candidate "
        "re-evaluated on the file's OWN modify watch (arm a watch and re-check, "
        "as _classify_and_step already does for 'unknown'), not only at create."
    ),
)
@pytest.mark.asyncio
async def test_contentful_stillborn_sibling_does_not_steal_the_live_transcript(env):
    """The 2026-08-14 auto-0730-021228 incident, with its real filenames.

    Two short-lived `claude` processes started at 01:25:26 and 01:25:31,
    each wrote its own <uuid>.jsonl carrying a queued message and a couple
    of 'Not logged in - Please run /login' turns, then died. The monitor
    linked to each in turn and abandoned a 13.7 MB transcript that was
    still being appended to. It stayed on the second corpse for ten hours
    while the session went on working; the operator's viewer froze at 01:25.

    N2 does not cover this: those files were NOT empty. They were 12,478
    bytes with a fresh sessionId, which is exactly what a legitimate
    rollover successor looks like. Nothing distinguishes 'this session
    rolled over' from 'a different, doomed process wrote in the same
    directory' -- so the newest contentful file wins, and the incumbent's
    own liveness is never consulted.
    """
    name = "auto-0730-021228"
    sdir = env.tmp_path / name / "sessions" / "-workspace-repo"
    env.make_session(name, sdir)

    live = sdir / "a7826859-a4a5-4ce3-b2fc-0617dfddb960.jsonl"
    live.write_text("\n".join(
        _claude_line(f"live content line {i}") for i in range(50)) + "\n")

    mon = env.make_monitor()
    mon.observe_rollout(name, live, source="reconciliation")
    await _settle(mon)
    assert _db_row(env.db_path, name)["jsonl_path"] == str(live)

    for stub_uuid, ts in (
        ("201026a4-3634-4a53-a8be-2d62d1aa5ce9", "2026-08-14T05:25:26.562Z"),
        ("017bced4-5d89-4686-b8eb-28d26533e776", "2026-08-14T05:25:31.461Z"),
    ):
        stub = sdir / f"{stub_uuid}.jsonl"
        stub.write_text(
            _claude_queue_line(stub_uuid, ts) + "\n"
            + _claude_line("Not logged in - Please run /login", ts=ts) + "\n"
        )
        # Born under inotify IN_CREATE, as they were in production: this
        # is what makes a file a supersede candidate (observe_rollout:2203).
        mon.observe_rollout(name, stub, source="watch_scan", create_event=True)
        await _settle(mon)
        # ...and the incumbent keeps growing, as it really did.
        with live.open("a") as fh:
            fh.write(_claude_line("live line after the stub appeared") + "\n")
        await _settle(mon)

    row = _db_row(env.db_path, name)
    assert row["jsonl_path"] == str(live), (
        "a sibling transcript must not take the link from an incumbent that "
        "is still being written, however much content the sibling carries"
    )


@pytest.mark.asyncio
async def test_real_rollover_is_still_adopted(env):
    """POSITIVE half of the pair. MUST run beside the reject case.

    The reject test alone cannot tell "fixed" from "disabled": a change that
    blocks ALL adoption satisfies it perfectly. That is not hypothetical --
    a parentUuid-presence gate did exactly that, went green here, and broke
    test_cross_boundary and test_sse_delivery, which live in other files and
    were not in the subset being run.

    A real rollover references the predecessor from its early entries
    (measured: 1 hit inside the first 50 on every real rollover pair on this
    host). It must still take the link.
    """
    name = "auto-rollover-ok"
    sdir = env.tmp_path / name / "sessions" / "-workspace-repo"
    env.make_session(name, sdir)

    anchor = "11111111-2222-3333-4444-555555555555"
    live = sdir / "aaaaaaaa-0000-0000-0000-000000000000.jsonl"
    live.write_text("\n".join(
        [json.dumps({"type": "mode", "timestamp": "2026-08-14T05:00:00.000Z"})]
        + [_claude_line(f"predecessor line {i}") for i in range(20)]
        + [json.dumps({"type": "assistant", "uuid": anchor,
                       "timestamp": "2026-08-14T05:10:00.000Z",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": "last turn"}]}})]
    ) + "\n")

    mon = env.make_monitor()
    mon.observe_rollout(name, live, source="reconciliation")
    await _settle(mon)
    assert _db_row(env.db_path, name)["jsonl_path"] == str(live)

    # A genuine rollover: its first entry continues the predecessor's chain.
    successor = sdir / "bbbbbbbb-0000-0000-0000-000000000000.jsonl"
    successor.write_text(json.dumps({
        "type": "user", "parentUuid": anchor,
        "sessionId": "bbbbbbbb-0000-0000-0000-000000000000",
        "timestamp": "2026-08-14T05:11:00.000Z",
        "message": {"role": "user", "content": [{"type": "text",
                                                 "text": "after compaction"}]},
    }) + "\n")
    mon.observe_rollout(name, successor, source="watch_scan", create_event=True)
    await _settle(mon)

    assert _db_row(env.db_path, name)["jsonl_path"] == str(successor), (
        "a successor that continues the linked conversation MUST be adopted; "
        "rejecting it breaks every real rollover"
    )
