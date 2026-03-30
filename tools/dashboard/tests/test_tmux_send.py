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
