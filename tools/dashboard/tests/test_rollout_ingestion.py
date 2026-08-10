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
    dispatch mapping, so a queued stale event can never dispatch into
    whichever track later recycles the wd."""
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
    assert mon._wd_to_track[wd] == (name, str(f), epoch)

    mon._release_track_watch(track)
    assert track.wd is None
    assert track.wd_epoch > epoch
    assert wd not in mon._wd_to_track


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
    """A5/B5: cancelling the awaiter does not stop the executor worker —
    the gate releases only when the worker actually finishes, with
    responsibility retained (no stranding, no double-tail)."""
    name = "auto-a5"
    m1 = _link_single(env, name, "a5-line")
    mon = env.make_monitor()

    real_read = mon._read_tail_window
    release_worker = {"t": 0.3}

    def slow_read(row):
        time.sleep(release_worker["t"])
        return real_read(row)

    monkeypatch.setattr(mon, "_read_tail_window", slow_read)
    mon.request_drain(name)
    await asyncio.sleep(0.05)
    gate = mon._gate(name)
    assert gate.busy

    for t in asyncio.all_tasks():
        coro = t.get_coro()
        if getattr(coro, "__qualname__", "").endswith("_drain_as_owner"):
            t.cancel()
    await asyncio.sleep(0.05)
    # The worker thread is still inside its read: the gate must NOT be
    # released yet (a finally-release here admits a double-tail).
    assert gate.busy, "gate released while the worker was still running"

    release_worker["t"] = 0.0   # continuation reads run fast
    await asyncio.sleep(0.4)    # original worker finishes → release + retry
    await _settle(mon, rounds=20)

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
