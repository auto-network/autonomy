"""Unified tmux paste-buffer injection with per-session lock and double-Enter retry.

All tmux message injection MUST go through tmux_send() (async) or
tmux_send_sync() (sync).  No other code should call paste-buffer or
send-keys directly.

Design:
  - Per-session asyncio.Lock serialises sends to the same tmux session.
  - Double Enter: first \r at 0.3s after paste, retry \r at 0.8s.
    If the first worked the retry hits an empty prompt (harmless).
    If it was dropped, the retry unsticks the submission.
  - tmux_send() spawns a background task and returns immediately.
  - tmux_send_sync() schedules from synchronous code (thread-safe).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import tempfile
import time

_session_locks: dict[str, asyncio.Lock] = {}


async def tmux_send(target: str, text: str) -> None:
    """Queue a paste+Enter to a tmux session.  Returns immediately."""
    asyncio.create_task(_tmux_send_worker(target, text))


async def _tmux_send_worker(target: str, text: str) -> None:
    lock = _session_locks.setdefault(target, asyncio.Lock())
    async with lock:
        _tmux_paste(target, text)
        await asyncio.sleep(0.3)
        _tmux_enter(target)
        await asyncio.sleep(0.5)
        _tmux_enter(target)  # retry — harmless if already submitted


def tmux_send_sync(target: str, text: str) -> None:
    """Send from any context — async event loop or thread."""
    try:
        loop = asyncio.get_running_loop()
        # We're on the event loop — schedule as a task
        loop.create_task(tmux_send(target, text))
    except RuntimeError:
        # We're in a thread — no running loop, call subprocesses directly
        _tmux_paste(target, text)
        time.sleep(0.3)
        _tmux_enter(target)
        time.sleep(0.5)
        _tmux_enter(target)


def _tmux_paste(target: str, text: str) -> None:
    """Load text into a unique tmux buffer and paste with bracketed-paste mode."""
    buf = f"inject_{secrets.token_hex(4)}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        tmp_path = f.name
    try:
        subprocess.run(
            ["tmux", "load-buffer", "-b", buf, tmp_path], capture_output=True
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-p", "-b", buf, "-t", target],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buf], capture_output=True
        )
    finally:
        os.unlink(tmp_path)


def _tmux_enter(target: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "\r"], capture_output=True
    )
