"""Worker hand-off protocol: markers, env contract, activation wait."""

from __future__ import annotations

import asyncio
import os

import pytest

from tools.dashboard import worker_handoff as wh


def test_env_accessors_absent_by_default():
    env = {}
    assert wh.ready_marker_path(env) is None
    assert wh.predecessor_pid(env) is None


def test_predecessor_pid_rejects_garbage():
    assert wh.predecessor_pid({wh.PREDECESSOR_PID_ENV: "abc"}) is None
    assert wh.predecessor_pid({wh.PREDECESSOR_PID_ENV: "0"}) is None
    assert wh.predecessor_pid({wh.PREDECESSOR_PID_ENV: "42"}) == 42


def test_mark_ready_noop_without_supervisor():
    assert wh.mark_ready({}) is None


def test_mark_ready_writes_pid_atomically(tmp_path):
    marker = tmp_path / "nested" / "w.ready"
    assert wh.mark_ready({wh.READY_MARKER_ENV: str(marker)}) == marker
    assert marker.read_text().strip() == str(os.getpid())
    assert not marker.with_name(marker.name + ".tmp").exists()


def test_activation_marker_and_cleanup(tmp_path):
    marker = tmp_path / "w.ready"
    wh.mark_ready({wh.READY_MARKER_ENV: str(marker)})
    activate = wh.signal_activation(marker)
    assert activate == tmp_path / "w.ready.activate"
    assert activate.exists()
    wh.cleanup_markers(marker)
    assert not marker.exists() and not activate.exists()
    wh.cleanup_markers(marker)  # idempotent


def test_pid_alive_semantics():
    import subprocess

    assert wh.pid_alive(os.getpid())
    # PID 1 exists but is not ours in most sandboxes: EPERM must read as alive.
    assert wh.pid_alive(1)
    # A reaped child: os.kill raises ESRCH.
    proc = subprocess.Popen(["true"])
    proc.wait()
    assert not wh.pid_alive(proc.pid)


def test_wait_for_activation_immediate_without_predecessor():
    reason = asyncio.run(wh.wait_for_activation(environ={}))
    assert reason == wh.ACTIVATED_NO_PREDECESSOR


def test_wait_for_activation_resolves_on_marker(tmp_path):
    marker = tmp_path / "w.ready"
    env = {wh.READY_MARKER_ENV: str(marker), wh.PREDECESSOR_PID_ENV: str(os.getpid())}
    polls = []

    def alive(pid):
        polls.append(pid)
        if len(polls) == 3:
            wh.signal_activation(marker)
        return True

    reason = asyncio.run(
        wh.wait_for_activation(environ=env, poll_seconds=0.001, pid_alive_fn=alive)
    )
    assert reason == wh.ACTIVATED_BY_SUPERVISOR
    assert polls == [os.getpid()] * 3


def test_wait_for_activation_resolves_when_predecessor_vanishes(tmp_path):
    marker = tmp_path / "w.ready"
    env = {wh.READY_MARKER_ENV: str(marker), wh.PREDECESSOR_PID_ENV: "4242"}
    seen = iter([True, True, False])
    reason = asyncio.run(
        wh.wait_for_activation(
            environ=env, poll_seconds=0.001, pid_alive_fn=lambda _pid: next(seen)
        )
    )
    assert reason == wh.ACTIVATED_PREDECESSOR_GONE


@pytest.mark.parametrize("missing", [wh.READY_MARKER_ENV, wh.PREDECESSOR_PID_ENV])
def test_wait_for_activation_needs_both_env_vars(tmp_path, missing):
    env = {wh.READY_MARKER_ENV: str(tmp_path / "w.ready"), wh.PREDECESSOR_PID_ENV: "4242"}
    env.pop(missing)
    assert asyncio.run(wh.wait_for_activation(environ=env)) == wh.ACTIVATED_NO_PREDECESSOR
