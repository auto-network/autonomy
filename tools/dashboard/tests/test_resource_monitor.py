"""Tests for the session resource metrics collector (resource_monitor)."""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from tools.dashboard.resource_monitor import (
    ResourceMonitor,
    _CgroupReader,
    _SessionState,
    _du_bytes,
    _parse_docker_size,
    _proc_tree_sample,
    _run_dir_for,
)


# ── unit helpers ────────────────────────────────────────────────


def test_parse_docker_size():
    assert _parse_docker_size("12.3MB (virtual 4.5GB)") == 12_300_000
    assert _parse_docker_size("0B (virtual 1.2GB)") == 0
    assert _parse_docker_size("456kB") == 456_000
    assert _parse_docker_size("1.5GiB") == int(1.5 * 2**30)
    assert _parse_docker_size("garbage") is None


def test_du_bytes_counts_tree(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 10_000)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b").write_bytes(b"y" * 20_000)
    total = _du_bytes(tmp_path)
    # st_blocks rounds up to block granularity — at least the data, and
    # not wildly more (two files < 64KiB apparent).
    assert total >= 30_000
    assert total < 200_000


def test_du_bytes_missing_path(tmp_path):
    assert _du_bytes(tmp_path / "nope") == 0


def test_run_dir_for():
    row = {"resolution_dir":
           "/x/data/agent-runs/auto-1-999/sessions/autonomy/uuid-1"}
    assert _run_dir_for(row) == Path("/x/data/agent-runs/auto-1-999")
    assert _run_dir_for({"jsonl_path": "/home/j/.claude/p/s.jsonl"}) is None
    assert _run_dir_for({}) is None


def test_proc_tree_sample_self():
    """Sampling our own process works and returns sane numbers."""
    cpu_ns, rss = _proc_tree_sample(os.getpid())
    assert cpu_ns > 0
    assert rss > 1024 * 1024  # a python process holds > 1MiB


def test_proc_tree_sample_gone_pid():
    cpu_ns, rss = _proc_tree_sample(2**22 + 12345)
    assert (cpu_ns, rss) == (0, 0)


# ── cgroup reader against a fake tree ───────────────────────────


CID = "abc123def456"


def _make_v2_tree(root: Path, scope: str) -> Path:
    d = root / scope
    d.mkdir(parents=True)
    (d / "cpu.stat").write_text(
        "usage_usec 5000000\nuser_usec 3000000\nsystem_usec 2000000\n")
    (d / "memory.current").write_text("104857600\n")
    (d / "memory.stat").write_text("anon 90000000\ninactive_file 4857600\n")
    return d


def test_cgroup_v2_systemd(tmp_path, monkeypatch):
    _make_v2_tree(tmp_path, f"system.slice/docker-{CID}.scope")
    monkeypatch.setattr(_CgroupReader, "ROOT", tmp_path)
    reader = _CgroupReader.probe(CID)
    assert reader is not None
    assert reader.backend == "v2-systemd"
    assert reader.read_cpu_ns() == 5_000_000_000
    assert reader.read_mem_bytes() == 104_857_600 - 4_857_600


def test_cgroup_v2_cgroupfs(tmp_path, monkeypatch):
    _make_v2_tree(tmp_path, f"docker/{CID}")
    monkeypatch.setattr(_CgroupReader, "ROOT", tmp_path)
    reader = _CgroupReader.probe(CID)
    assert reader is not None
    assert reader.backend == "v2-cgroupfs"


def test_cgroup_v1(tmp_path, monkeypatch):
    cpu = tmp_path / f"cpu,cpuacct/docker/{CID}"
    cpu.mkdir(parents=True)
    (cpu / "cpuacct.usage").write_text("7000000000\n")
    mem = tmp_path / f"memory/docker/{CID}"
    mem.mkdir(parents=True)
    (mem / "memory.usage_in_bytes").write_text("50000000\n")
    (mem / "memory.stat").write_text("total_inactive_file 10000000\n")
    monkeypatch.setattr(_CgroupReader, "ROOT", tmp_path)
    reader = _CgroupReader.probe(CID)
    assert reader is not None
    assert reader.backend == "v1"
    assert reader.read_cpu_ns() == 7_000_000_000
    assert reader.read_mem_bytes() == 40_000_000


def test_cgroup_probe_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(_CgroupReader, "ROOT", tmp_path)
    assert _CgroupReader.probe(CID) is None


# ── tick behavior ───────────────────────────────────────────────


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    """A monitor with worktrees under tmp and DB writes captured."""
    m = ResourceMonitor(cpu_interval=6,
                        worktrees_dir=tmp_path / "worktrees")
    writes: list[tuple] = []
    monkeypatch.setattr(
        "tools.dashboard.resource_monitor.update_disk_usage",
        lambda *a: writes.append(a))
    m._test_writes = writes
    return m


def _host_row(tmp_path, name="auto-t1", entry_count=1, created_at=None):
    run_dir = tmp_path / "agent-runs" / f"{name}-123"
    res_dir = run_dir / "sessions" / "autonomy" / "uuid-1"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / "log.jsonl").write_bytes(b"z" * 5000)
    return {
        "tmux_name": name,
        "type": "host",
        "resolution_dir": str(res_dir),
        "jsonl_path": str(res_dir / "log.jsonl"),
        "startup_state": None,
        "entry_count": entry_count,
        "created_at": created_at if created_at is not None else time.time(),
    }


def _prime_host_state(m, name):
    """Pre-resolve a session as a host handle on our own pid."""
    s = _SessionState(kind="host", backend="proc", pane_pid=os.getpid())
    m._states[name] = s
    return s


def test_tick_samples_cpu_mem(monitor, tmp_path):
    row = _host_row(tmp_path)
    _prime_host_state(monitor, row["tmux_name"])
    now = time.time()
    monitor._tick([row], now)
    monitor._tick([row], now + 6)
    s = monitor._states[row["tmux_name"]]
    assert s.mem_bytes > 0
    assert s.cpu_pct is not None and s.cpu_pct >= 0
    assert len(s.history) >= 1


def test_disk_staggered_one_per_tick(monitor, tmp_path):
    rows = [_host_row(tmp_path, f"auto-t{i}") for i in range(3)]
    for r in rows:
        _prime_host_state(monitor, r["tmux_name"])
    now = time.time()
    monitor._tick(rows, now)
    assert len(monitor._test_writes) == 1  # one disk measure only
    monitor._tick(rows, now + 6)
    assert len(monitor._test_writes) == 2  # next session's turn
    # measured sessions have all class clocks scheduled into the future
    measured = {w[0] for w in monitor._test_writes}
    for name in measured:
        for due in monitor._states[name].next_disk_at.values():
            assert due > now + 30


def test_disk_measure_components(monitor, tmp_path):
    row = _host_row(tmp_path)
    name = row["tmux_name"]
    state = _prime_host_state(monitor, name)
    wt = tmp_path / "worktrees" / name / "repo"
    wt.mkdir(parents=True)
    (wt / "f").write_bytes(b"w" * 8000)
    disk = monitor._scan_disk(row, state, time.time())
    assert set(disk["components"]) == {"run_dir", "worktrees"}
    assert disk["total"] == sum(disk["components"].values())
    assert disk["total"] >= 13_000
    assert "run_dir" in disk["timings_ms"]


def test_idle_session_stops_scanning(monitor, tmp_path):
    """No new turns since last scan → no disk walk at all."""
    row = _host_row(tmp_path, entry_count=5)
    state = _prime_host_state(monitor, row["tmux_name"])
    now = time.time()
    assert monitor._scan_disk(row, state, now) is not None
    # both class clocks come due again, but entry_count is unchanged
    assert monitor._scan_disk(row, state, now + 10_000) is None
    assert len(monitor._test_writes) == 1
    # a new turn re-enables scanning
    row["entry_count"] = 6
    assert monitor._scan_disk(row, state, now + 20_000) is not None
    assert len(monitor._test_writes) == 2


def test_cadence_by_class_and_age(monitor, tmp_path):
    now = time.time()
    young = _host_row(tmp_path, "auto-young", created_at=now - 60)
    old = _host_row(tmp_path, "auto-old", created_at=now - 7200)
    ys = _prime_host_state(monitor, "auto-young")
    os_ = _prime_host_state(monitor, "auto-old")
    monitor._scan_disk(young, ys, now)
    monitor._scan_disk(old, os_, now)
    # young: fast=60s heavy=300s; old: fast=300s heavy=1800s
    assert ys.next_disk_at["fast"] == pytest.approx(now + 60, abs=1)
    assert ys.next_disk_at["heavy"] == pytest.approx(now + 300, abs=1)
    assert os_.next_disk_at["fast"] == pytest.approx(now + 300, abs=1)
    assert os_.next_disk_at["heavy"] == pytest.approx(now + 1800, abs=1)


def test_partial_scan_merges_components(monitor, tmp_path):
    """A fast-only rescan keeps the last heavy components in the total."""
    row = _host_row(tmp_path, entry_count=1)
    name = row["tmux_name"]
    state = _prime_host_state(monitor, name)
    wt = tmp_path / "worktrees" / name
    wt.mkdir(parents=True)
    (wt / "f").write_bytes(b"w" * 8000)
    now = time.time()
    full = monitor._scan_disk(row, state, now)
    assert set(full["components"]) == {"run_dir", "worktrees"}
    # only fast is due 90s later (young session: fast=60, heavy=300)
    row["entry_count"] = 2
    merged = monitor._scan_disk(row, state, now + 90)
    assert set(merged["components"]) == {"run_dir", "worktrees"}
    assert merged["total"] == sum(merged["components"].values())
    assert state.next_disk_at["heavy"] == pytest.approx(now + 300, abs=1)


def test_force_refresh_bypasses_clocks_and_idle_skip(monitor, tmp_path):
    row = _host_row(tmp_path, entry_count=3)
    state = _prime_host_state(monitor, row["tmux_name"])
    now = time.time()
    monitor._scan_disk(row, state, now)
    # idle + nothing due → a normal scan is a no-op, force still measures
    assert monitor._scan_disk(row, state, now + 1) is None
    disk = monitor._scan_disk(row, state, now + 2, force=True)
    assert disk is not None and "run_dir" in disk["components"]
    assert len(monitor._test_writes) == 2


def test_booting_sessions_not_polled(monitor, tmp_path):
    row = _host_row(tmp_path)
    row["startup_state"] = "launching_container"
    monitor._tick([row], time.time())
    assert row["tmux_name"] not in monitor._states


def test_dead_sessions_dropped_from_poll_set(monitor, tmp_path):
    row = _host_row(tmp_path)
    _prime_host_state(monitor, row["tmux_name"])
    monitor._tick([row], time.time())
    monitor._tick([], time.time() + 6)  # no longer live
    assert monitor._states == {}


def test_finalize_persists_and_stops_tracking(monitor, tmp_path):
    row = _host_row(tmp_path)
    name = row["tmux_name"]
    _prime_host_state(monitor, name)
    monitor._finalize(name, row)
    assert name not in monitor._states
    assert len(monitor._test_writes) == 1
    wname, wbytes, wdetail, wts = monitor._test_writes[0]
    assert wname == name
    assert wbytes > 0
    detail = json.loads(wdetail)
    # final measure never includes container storage — the container is gone
    assert "container_fs" not in detail["components"]
    assert detail["total"] == wbytes


def test_health_reports_costs(monitor, tmp_path):
    row = _host_row(tmp_path)
    _prime_host_state(monitor, row["tmux_name"])
    monitor._tick([row], time.time())
    health = monitor.get_health()
    assert health["tick"]["count"] == 1
    assert health["tick"]["max_ms"] >= 0
    assert health["disk_measure"]["count"] == 1
    assert health["tracked"] == 1
    snap = monitor.snapshot(include_history=True)
    assert row["tmux_name"] in snap["sessions"]
    assert "history" in snap["sessions"][row["tmux_name"]]


def test_unresolved_backoff(monitor, tmp_path, monkeypatch):
    """Resolution failure backs off instead of hammering subprocesses."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        raise OSError("no docker, no tmux")

    monkeypatch.setattr(
        "tools.dashboard.resource_monitor.subprocess.run", fake_run)
    row = _host_row(tmp_path)
    now = time.time()
    monitor._tick([row], now)
    n_first = len(calls)
    assert n_first >= 1
    monitor._tick([row], now + 1)  # inside backoff window
    assert len(calls) == n_first
    state = monitor._states[row["tmux_name"]]
    assert state.kind == "unresolved"
    assert state.next_resolve_at > now


# ── API endpoint ────────────────────────────────────────────────


def test_api_resources_endpoint(tmp_path):
    from starlette.testclient import TestClient

    from tools.dashboard import server as srv

    client = TestClient(srv.app)  # no lifespan — handler needs no startup
    resp = client.get("/api/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert "sessions" in body and "health" in body
    assert "tick" in body["health"]


# ── DB migration + persistence roundtrip ────────────────────────


def test_disk_columns_migration_and_update(tmp_path):
    from tools.dashboard.dao import dashboard_db as db

    db_path = tmp_path / "dash.db"
    old_conn = db._conn
    old_path = db._DB_PATH
    try:
        db._conn = None
        db._DB_PATH = db_path
        db.init_db(db_path)
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO tmux_sessions (tmux_name, type, project, created_at)"
            " VALUES ('auto-x', 'host', 'autonomy', 1.0)")
        conn.commit()
        db.update_disk_usage("auto-x", 12345, '{"total": 12345}', 99.0)
        row = db.get_session("auto-x")
        assert row["disk_bytes"] == 12345
        assert json.loads(row["disk_detail"])["total"] == 12345
        assert row["disk_sampled_at"] == 99.0
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = old_conn
        db._DB_PATH = old_path
