"""Deliver a CrossTalk message on behalf of a non-session participant.

A ChatGPT chat has no tmux pane and no session token; it is identified by the
minted handle on its ``mcp_sessions`` row (``ChatGPT-<datetime>``). The dashboard
— never the relay — performs delivery and stamps the source from that handle, so
attribution does not depend on which process or token launched the relay. That is
the fix for the wrong-source defect: the deliverer sets the source, and the
deliverer is the dashboard, which knows the true sender.

A message to a live tmux target is pasted and recorded ``delivered=1``; a message
to any non-live target (another chat's handle, an offline participant) is stored
``delivered=0`` for its owner to collect later. Both are one row in
``crosstalk_messages`` — the single store.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

from tools.dashboard.dao import auth_db
from tools.dashboard.tmux_send import tmux_send


def _tmux_session_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0


def render_envelope(*, from_id: str, label: str, message: str,
                    source_id: str = "", turn: int | None = None,
                    harness: str = "chatgpt", model: str = "") -> str:
    """The CrossTalk block a target pane sees. `from_id` is the authoritative
    source — for a chat it is the minted handle, set by the dashboard, not the
    relay."""
    turn_str = str(turn) if turn is not None else ""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return (f'<crosstalk from="{from_id}"\n'
            f'           label="{label}"\n'
            f'           source="{source_id}" turn="{turn_str}"\n'
            f'           harness="{harness}" model="{model}"\n'
            f'           timestamp="{ts}">\n'
            f'{message}\n</crosstalk>')


async def deliver_from_chat(handle: str, to: str, message: str) -> dict:
    """Deliver `message` to `to`, sourced from the chat `handle`.

    Live tmux target → paste the envelope (source stamped = handle) and record
    delivered=1. Non-live target → store delivered=0 (queued) for collection. The
    source is always `handle`, never the relay's launching identity.
    """
    live = _tmux_session_exists(to)
    if live:
        await tmux_send(to, render_envelope(from_id=handle, label=handle, message=message))
    await asyncio.to_thread(
        auth_db.insert_message, handle, handle, to, None, None, message, time.time(),
        1 if live else 0)
    return {"delivered": live, "queued": not live}
