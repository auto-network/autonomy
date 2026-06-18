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


class TmuxSendError(Exception):
    """Raised by :func:`tmux_send_awaited` when one of the underlying
    tmux subprocesses returns a non-zero exit code.

    Carries the named operation (``load-buffer`` / ``paste-buffer`` /
    ``delete-buffer`` / ``send-keys``), the subprocess returncode,
    and the captured stderr so the caller can render an actionable
    error to the operator (e.g. ``/ws/voice`` surfacing a
    ``commit_error`` frame with the underlying tmux failure).

    The classic fire-and-forget :func:`tmux_send` never raises this
    — it intentionally drops subprocess outcomes on the floor.
    """

    def __init__(self, op: str, returncode: int, stderr: str):
        self.op = op
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"tmux {op} failed (returncode={returncode}): "
            f"{stderr or '<no stderr>'}"
        )


async def tmux_send(target: str, text: str) -> None:
    """Queue a paste+Enter to a tmux session.  Returns immediately.

    The actual paste runs in a background task; this function does
    NOT block on completion and does NOT raise on tmux failure.
    For callers that need success/failure to surface (the
    ``/ws/voice`` commit path, for instance), use
    :func:`tmux_send_awaited` instead.
    """
    asyncio.create_task(_tmux_send_worker(target, text))


async def tmux_send_awaited(target: str, text: str) -> None:
    """Synchronous paste+Enter that awaits the actual subprocess
    completion AND raises :class:`TmuxSendError` on any non-zero
    tmux return code.

    Same per-session locking and double-Enter retry as
    :func:`tmux_send`; the difference is the success boundary.
    With ``tmux_send`` the boundary is "we scheduled a worker";
    with ``tmux_send_awaited`` the boundary is "tmux accepted the
    paste and the first enter landed without error". Use this
    where the operator-visible success of the operation depends
    on the actual tmux outcome (``/ws/voice`` commit) rather than
    on a fire-and-forget delivery promise.

    Note: the retry enter is best-effort — if the first enter
    succeeded (returncode 0) we don't surface a failure from the
    retry. The retry exists to unstick a missed first enter, not
    to be an independent reliability signal.
    """
    lock = _session_locks.setdefault(target, asyncio.Lock())
    async with lock:
        _tmux_paste_checked(target, text)
        await asyncio.sleep(0.3)
        _tmux_enter_checked(target)
        await asyncio.sleep(0.5)
        # Retry enter — best-effort, do not raise even on failure.
        # If the first enter succeeded, this hits an empty prompt;
        # if it failed, we already raised above.
        try:
            _tmux_enter_checked(target)
        except TmuxSendError:
            pass


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
    """Load text into a unique tmux buffer and paste with bracketed-paste mode.

    Subprocess outcomes are intentionally dropped — paired with
    :func:`tmux_send` / :func:`tmux_send_sync` which are
    fire-and-forget by contract. Callers that need failure
    detection use :func:`_tmux_paste_checked` instead (paired
    with :func:`tmux_send_awaited`)."""
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
    """Fire an Enter to the tmux target. Subprocess outcome dropped
    — see :func:`_tmux_paste`'s docstring."""
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "\r"], capture_output=True
    )


def _check_subprocess(op: str, result: subprocess.CompletedProcess) -> None:
    """Raise :class:`TmuxSendError` if ``result.returncode != 0``.

    Extracted helper so both ``_tmux_paste_checked`` and
    ``_tmux_enter_checked`` raise with identical shape (named op +
    returncode + decoded stderr). The stderr decode is lossy on
    purpose — operator-readable matters more than byte-fidelity
    here."""
    if result.returncode == 0:
        return
    stderr = ""
    if result.stderr:
        try:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
        except Exception:
            stderr = repr(result.stderr)[:200]
    raise TmuxSendError(op, result.returncode, stderr)


def _tmux_paste_checked(target: str, text: str) -> None:
    """Like :func:`_tmux_paste` but raises :class:`TmuxSendError`
    on any non-zero subprocess returncode. Used by
    :func:`tmux_send_awaited`."""
    buf = f"inject_{secrets.token_hex(4)}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        tmp_path = f.name
    try:
        _check_subprocess(
            "load-buffer",
            subprocess.run(
                ["tmux", "load-buffer", "-b", buf, tmp_path],
                capture_output=True,
            ),
        )
        _check_subprocess(
            "paste-buffer",
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-b", buf, "-t", target],
                capture_output=True,
            ),
        )
        # delete-buffer is best-effort cleanup; tmux may have
        # already discarded the buffer in some race cases. Don't
        # raise on its failure — the paste already landed.
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buf], capture_output=True
        )
    finally:
        os.unlink(tmp_path)


def tmux_paste_checked_sync(target: str, text: str, *, timeout: float = 5.0) -> None:
    """Paste text synchronously and raise on tmux failure.

    This is the lifecycle worker's lower-level primitive for echo-verified
    injection: paste first, let the caller inspect the pane, then press Enter
    only after the prompt visibly contains input.
    """
    buf = f"inject_{secrets.token_hex(4)}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        tmp_path = f.name
    try:
        _check_subprocess(
            "load-buffer",
            subprocess.run(
                ["tmux", "load-buffer", "-b", buf, tmp_path],
                capture_output=True,
                timeout=timeout,
            ),
        )
        _check_subprocess(
            "paste-buffer",
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-b", buf, "-t", target],
                capture_output=True,
                timeout=timeout,
            ),
        )
        subprocess.run(
            ["tmux", "delete-buffer", "-b", buf],
            capture_output=True,
            timeout=timeout,
        )
    finally:
        os.unlink(tmp_path)


def _tmux_enter_checked(target: str) -> None:
    """Like :func:`_tmux_enter` but raises :class:`TmuxSendError`
    on non-zero returncode. Used by :func:`tmux_send_awaited`."""
    _check_subprocess(
        "send-keys",
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "\r"],
            capture_output=True,
        ),
    )


def tmux_enter_checked_sync(target: str, *, timeout: float = 5.0) -> None:
    """Press Enter synchronously and raise on tmux failure."""
    _check_subprocess(
        "send-keys",
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "\r"],
            capture_output=True,
            timeout=timeout,
        ),
    )


# auto-eerfx: raw key-event injection for TUI overlays (trust dialog,
# planning-mode toggles, etc.). The paste-buffer + double-Enter dance
# above is sized for *message bodies*; it can't deliver Enter alone, an
# arrow key, or a single digit. tmux_send_keys is the dedicated helper
# for control sequences. Shares the per-session lock with tmux_send so a
# user-typed message can't interleave with an in-flight confirm.

# Each keystroke is a dict: {"kind": "key"|"literal", "value": str}.
#  - kind="key"     → ``tmux send-keys -t <target> <value>`` (e.g. "C-m"
#                     for Enter, "Down" for an arrow, "Escape" for ESC).
#                     Standard tmux key syntax.
#  - kind="literal" → ``tmux send-keys -t <target> -l <value>`` (literal
#                     bytes, no interpretation; for digits, letters,
#                     short strings).
async def tmux_send_keys(target: str, keystrokes: list[dict]) -> None:
    """Inject a sequence of raw key events into a tmux session.

    Returns immediately. The work runs under the same per-session lock
    as ``tmux_send`` so message-body paste cannot interleave with a
    confirm-sequence injection mid-stream.
    """
    asyncio.create_task(_tmux_send_keys_worker(target, list(keystrokes)))


async def _tmux_send_keys_worker(target: str, keystrokes: list[dict]) -> None:
    lock = _session_locks.setdefault(target, asyncio.Lock())
    async with lock:
        for ks in keystrokes:
            _tmux_send_one_key(target, ks)
            # Small inter-key gap matches the cadence of a human pressing
            # keys in a TUI; some terminals coalesce overly-tight input.
            await asyncio.sleep(0.05)


def _tmux_send_one_key(target: str, keystroke: dict) -> None:
    kind = (keystroke or {}).get("kind")
    value = (keystroke or {}).get("value")
    if not value:
        return
    if kind == "literal":
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", value],
            capture_output=True,
        )
    else:
        # Default to non-literal — tmux's standard key syntax (C-m,
        # Down, Escape, etc.). Includes the explicit "key" kind.
        subprocess.run(
            ["tmux", "send-keys", "-t", target, value],
            capture_output=True,
        )
