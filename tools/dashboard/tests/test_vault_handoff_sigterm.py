"""auto-wb6ok: the vault reaches the replacement even when the hand-off
notice is lost and the incumbent is killed before its lifespan shutdown."""
from __future__ import annotations

import os
import signal
import threading

import pytest

from tools.dashboard import unlock_routes, worker_handoff


@pytest.fixture
def sigterm_restored():
    original = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, original)


def test_sigterm_snapshots_once_then_chains_to_the_previous_handler(sigterm_restored):
    order = []
    signal.signal(signal.SIGTERM, lambda sig, frame: order.append("uvicorn"))
    assert worker_handoff.install_snapshot_on_sigterm(lambda: order.append("snapshot"))
    os.kill(os.getpid(), signal.SIGTERM)
    os.kill(os.getpid(), signal.SIGTERM)
    assert order == ["snapshot", "uvicorn", "uvicorn"]


def test_a_failing_snapshot_still_chains(sigterm_restored):
    order = []
    signal.signal(signal.SIGTERM, lambda sig, frame: order.append("uvicorn"))

    def boom():
        raise RuntimeError("no carrier")
    worker_handoff.install_snapshot_on_sigterm(boom)
    os.kill(os.getpid(), signal.SIGTERM)
    assert order == ["uvicorn"]


def test_install_refuses_off_the_main_thread():
    got = []
    t = threading.Thread(target=lambda: got.append(
        worker_handoff.install_snapshot_on_sigterm(lambda: None)))
    t.start()
    t.join()
    assert got == [False]


def test_sigterm_snapshot_never_waits_on_a_held_lock(monkeypatch):
    """The handler interrupts arbitrary code; if a snapshot is already being
    written (the lock is held), it skips instead of deadlocking."""
    wrote = []
    monkeypatch.setattr(unlock_routes, "_save_vault_snapshot", lambda: wrote.append(1) or True)
    assert unlock_routes._SNAPSHOT_LOCK.acquire()
    try:
        assert unlock_routes.save_vault_on_sigterm() is False
    finally:
        unlock_routes._SNAPSHOT_LOCK.release()
    assert wrote == []
    assert unlock_routes.save_vault_on_sigterm() is True
    assert wrote == [1]
