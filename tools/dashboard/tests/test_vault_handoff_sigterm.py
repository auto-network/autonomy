"""auto-wb6ok: the vault reaches the replacement even when the hand-off
notice is lost and the incumbent is killed before its lifespan shutdown."""
from __future__ import annotations

import os
import signal
import threading

import pytest

from tools.dashboard import unlock_routes, vault_handoff_attention, worker_handoff


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


def test_notice_failure_is_read_from_the_supervisor_env():
    env = {worker_handoff.NOTICE_FAILURE_ENV: "TimeoutError: timed out"}
    assert worker_handoff.notice_failure(env) == "TimeoutError: timed out"
    assert worker_handoff.notice_failure({}) is None


@pytest.mark.parametrize(("warm", "failure", "state", "title"), [
    (False, None, "needs_attention", "Vault is locked after a dashboard reload"),
    (False, "TimeoutError: timed out", "needs_attention",
     "Vault is locked after a dashboard reload"),
    (True, "TimeoutError: timed out", "needs_attention",
     "Dashboard reload hand-off notice failed"),
    (True, None, "resolved", "Vault carried across the last reload"),
])
def test_attention_condition(warm, failure, state, title):
    c = vault_handoff_attention.derive_condition(
        warm=warm, notice_failure=failure, machine_id="m1", now=1000.0)
    assert c["attention_state"] == state and c["safe_title"] == title
    assert c["kind"] == "machine.vault_handoff_failed"
    assert c["attention_id"] == "machine:m1:vault-handoff"
    assert c["source_version"] == 1_000_000
    if failure and state == "needs_attention":
        assert failure in c["safe_summary"]


def test_attention_class_is_registered_with_a_runtime():
    from tools.dashboard.attention_registry import build_production_attention_registry
    registry = build_production_attention_registry(
        runtimes=vault_handoff_attention.publication_runtimes())
    assert registry.producer("machine.vault_handoff_failed", "machine") is not None
