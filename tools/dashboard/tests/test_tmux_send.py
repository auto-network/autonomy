"""Tests for tmux_send_sync context detection.

Verifies tmux_send_sync works correctly from:
1. Async context (on the event loop)
2. asyncio.to_thread (nag delivery path)
3. Plain threading.Thread
4. Error propagation (no silent failures)
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from tools.dashboard.tmux_send import tmux_send_sync


@pytest.fixture(autouse=True)
def _mock_subprocess():
    """Mock subprocess.run so tests don't need tmux."""
    with patch("tools.dashboard.tmux_send.subprocess.run") as mock_run:
        yield mock_run


@pytest.fixture(autouse=True)
def _mock_sleep():
    """Mock time.sleep so thread-path tests are fast."""
    with patch("tools.dashboard.tmux_send.time.sleep"):
        yield


def _assert_paste_and_enter(mock_run):
    """Verify _tmux_paste and _tmux_enter were called with correct args."""
    calls = mock_run.call_args_list
    # _tmux_paste makes 3 subprocess calls (load-buffer, paste-buffer, delete-buffer)
    # _tmux_enter makes 1 call each, called twice = 2
    # Total: 5 subprocess.run calls
    assert len(calls) == 5, f"Expected 5 subprocess.run calls, got {len(calls)}: {calls}"
    # First call is load-buffer
    assert "load-buffer" in calls[0].args[0]
    # Second call is paste-buffer with target
    assert "paste-buffer" in calls[1].args[0]
    assert "test-session" in calls[1].args[0]
    # Third is delete-buffer
    assert "delete-buffer" in calls[2].args[0]
    # Fourth and fifth are send-keys (Enter)
    assert calls[3].args[0] == ["tmux", "send-keys", "-t", "test-session", "\r"]
    assert calls[4].args[0] == ["tmux", "send-keys", "-t", "test-session", "\r"]


def test_async_context(_mock_subprocess):
    """From async context, tmux_send_sync schedules via create_task."""

    async def _run():
        tmux_send_sync("test-session", "hello from async")
        # The task is scheduled on the running loop — wait for the worker's
        # asyncio.sleep(0.3) + asyncio.sleep(0.5) to complete
        await asyncio.sleep(1.0)

    asyncio.run(_run())

    calls = _mock_subprocess.call_args_list
    assert len(calls) == 5, f"Expected 5 subprocess.run calls from async path, got {len(calls)}"
    assert "load-buffer" in calls[0].args[0]


def test_to_thread_context(_mock_subprocess):
    """From asyncio.to_thread (nag delivery path), uses direct subprocess calls."""

    async def _run():
        await asyncio.to_thread(tmux_send_sync, "test-session", "hello from to_thread")

    asyncio.run(_run())
    _assert_paste_and_enter(_mock_subprocess)


def test_plain_thread(_mock_subprocess):
    """From a plain threading.Thread, uses direct subprocess calls."""
    exc_holder = []

    def thread_fn():
        try:
            tmux_send_sync("test-session", "hello from thread")
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=thread_fn)
    t.start()
    t.join(timeout=5)

    assert not exc_holder, f"Thread raised: {exc_holder}"
    _assert_paste_and_enter(_mock_subprocess)


def test_no_silent_failure():
    """If _tmux_paste raises, the exception propagates — not swallowed."""
    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=OSError("tmux not found")):
        with patch("tools.dashboard.tmux_send.time.sleep"):
            with pytest.raises(OSError, match="tmux not found"):
                tmux_send_sync("test-session", "should fail")


# ── tmux_send_awaited (S3-5 F1) ────────────────────────────────


import subprocess
from tools.dashboard.tmux_send import tmux_send_awaited, TmuxSendError


def _ok(cmd):
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")


def _fail(cmd, stderr=b"tmux died"):
    return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=b"", stderr=stderr)


@pytest.mark.asyncio
async def test_tmux_send_awaited_success_runs_full_sequence(_mock_subprocess):
    """Awaited helper runs paste + first enter + retry enter inline.
    All subprocesses returncode=0 → completes without raising."""
    _mock_subprocess.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b"", stderr=b"",
    )
    with patch("tools.dashboard.tmux_send.asyncio.sleep"):
        await tmux_send_awaited("test-session", "all good")
    calls = _mock_subprocess.call_args_list
    # 3 (paste path: load-buffer / paste-buffer / delete-buffer) +
    # 2 (enters) = 5
    assert len(calls) == 5
    assert "load-buffer" in calls[0].args[0]
    assert "paste-buffer" in calls[1].args[0]
    assert "delete-buffer" in calls[2].args[0]
    assert calls[3].args[0] == ["tmux", "send-keys", "-t", "test-session", "\r"]
    assert calls[4].args[0] == ["tmux", "send-keys", "-t", "test-session", "\r"]


@pytest.mark.asyncio
async def test_tmux_send_awaited_raises_on_load_buffer_failure():
    """Regression for codex F1 on f2f1895: a failed tmux subprocess
    MUST surface as TmuxSendError so the route can emit
    commit_error rather than the misleading 'committed' frame."""
    def fake_run(cmd, *args, **kwargs):
        if "load-buffer" in cmd:
            return _fail(cmd, stderr=b"no such buffer")
        return _ok(cmd)

    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=fake_run), \
         patch("tools.dashboard.tmux_send.asyncio.sleep"):
        with pytest.raises(TmuxSendError) as exc_info:
            await tmux_send_awaited("test-session", "fails")
        assert exc_info.value.op == "load-buffer"
        assert exc_info.value.returncode == 1
        assert "no such buffer" in exc_info.value.stderr


@pytest.mark.asyncio
async def test_tmux_send_awaited_raises_on_paste_buffer_failure():
    def fake_run(cmd, *args, **kwargs):
        if "paste-buffer" in cmd:
            return _fail(cmd, stderr=b"target session vanished")
        return _ok(cmd)

    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=fake_run), \
         patch("tools.dashboard.tmux_send.asyncio.sleep"):
        with pytest.raises(TmuxSendError) as exc_info:
            await tmux_send_awaited("test-session", "fails")
        assert exc_info.value.op == "paste-buffer"
        assert "target session vanished" in exc_info.value.stderr


@pytest.mark.asyncio
async def test_tmux_send_awaited_raises_on_first_enter_failure():
    def fake_run(cmd, *args, **kwargs):
        if "send-keys" in cmd:
            return _fail(cmd, stderr=b"can't send keys")
        return _ok(cmd)

    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=fake_run), \
         patch("tools.dashboard.tmux_send.asyncio.sleep"):
        with pytest.raises(TmuxSendError) as exc_info:
            await tmux_send_awaited("test-session", "fails")
        assert exc_info.value.op == "send-keys"


@pytest.mark.asyncio
async def test_tmux_send_awaited_retry_enter_failure_does_not_raise():
    """The retry Enter is best-effort. If the first Enter succeeded
    and the retry fails, that's not an error — the retry exists to
    unstick a missed first enter, not to be an independent
    reliability signal."""
    call_count = {"send-keys": 0}

    def fake_run(cmd, *args, **kwargs):
        if "send-keys" in cmd:
            call_count["send-keys"] += 1
            if call_count["send-keys"] == 2:
                return _fail(cmd, stderr=b"retry failed")
            return _ok(cmd)
        return _ok(cmd)

    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=fake_run), \
         patch("tools.dashboard.tmux_send.asyncio.sleep"):
        # Must NOT raise even though the retry enter failed.
        await tmux_send_awaited("test-session", "retry-tolerated")
    assert call_count["send-keys"] == 2  # both fired


@pytest.mark.asyncio
async def test_tmux_send_awaited_delete_buffer_failure_does_not_raise():
    """delete-buffer is best-effort cleanup; tmux may have already
    discarded the buffer in some race cases. Don't raise on its
    failure — the paste already landed."""
    def fake_run(cmd, *args, **kwargs):
        if "delete-buffer" in cmd:
            return _fail(cmd, stderr=b"no such buffer")
        return _ok(cmd)

    with patch("tools.dashboard.tmux_send.subprocess.run", side_effect=fake_run), \
         patch("tools.dashboard.tmux_send.asyncio.sleep"):
        # Must NOT raise.
        await tmux_send_awaited("test-session", "ok")


def test_tmux_send_error_message_shape():
    err = TmuxSendError("load-buffer", 1, "boom")
    assert err.op == "load-buffer"
    assert err.returncode == 1
    assert err.stderr == "boom"
    assert "tmux load-buffer failed" in str(err)
    assert "returncode=1" in str(err)
    assert "boom" in str(err)


def test_tmux_send_error_empty_stderr_renders_placeholder():
    """When stderr is empty (some tmux failures provide none), the
    string representation falls back to '<no stderr>' rather than
    rendering an empty colon-suffix."""
    err = TmuxSendError("send-keys", 1, "")
    assert "<no stderr>" in str(err)
