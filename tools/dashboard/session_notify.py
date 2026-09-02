"""One idempotent, harness-neutral system notification into a session's tmux.

The ``<task-notification>`` envelope is understood by both the Claude and Codex
transcript adapters and is normalized ``type=system``, so it wakes the agent on
its next turn without registering as operator input. A stable notification id is
deduped for this dashboard process's lifetime so a retry — or a decision that
both wakes a held ``?wait=`` GET and posts a notification — cannot wake the
agent twice.

The envelope builder is the single home of that wire format; the HTTP route
(:func:`tools.dashboard.server.api_session_notify`) and the vault-open decision
wake share it rather than re-emitting the tags independently.
"""

from __future__ import annotations

import html as _html
import time

from tools.dashboard.crosstalk_delivery import _tmux_session_exists
from tools.dashboard.tmux_send import tmux_send_sync


def build_task_notification_envelope(
    notification_id: str,
    *,
    kind: str,
    status: str,
    summary: str,
    body: str = "",
) -> str:
    """The exact ``<task-notification>`` envelope both delivery paths emit."""
    esc = {
        "id": _html.escape(notification_id),
        "kind": _html.escape(kind),
        "status": _html.escape(status[:100]),
        "summary": _html.escape(summary),
        "body": _html.escape(body),
    }
    return (
        "<task-notification>\n"
        f"<id>{esc['id']}</id>\n"
        f"<kind>{esc['kind']}</kind>\n"
        f"<summary>{esc['summary']}</summary>\n"
        f"<status>{esc['status']}</status>\n"
        + (f"<body>{esc['body']}</body>\n" if body else "")
        + "</task-notification>"
    )


# Wake-side dedup for the approval-decision path. Independent of the HTTP route's
# map: the id namespaces do not collide (this path uses ``vault-open:<id>``), and
# a decision must never be able to wake its requester twice.
_WAKE_IDS: dict[tuple[str, str], float] = {}
_WAKE_MAX = 4096


def deliver_task_notification_sync(
    tmux_session: str,
    notification_id: str,
    *,
    kind: str,
    status: str,
    summary: str,
    body: str = "",
) -> str:
    """Best-effort, deduped wake into ``tmux_session``.

    Returns ``accepted`` / ``duplicate`` / ``absent`` / ``invalid`` / ``error``.
    Never raises: a wake failure must never roll back a committed decision, so
    every fault degrades to a status string the caller can log and ignore.
    """
    try:
        if not tmux_session or not notification_id or not summary:
            return "invalid"
        if not _tmux_session_exists(tmux_session):
            return "absent"
        key = (tmux_session, notification_id)
        if key in _WAKE_IDS:
            return "duplicate"
        envelope = build_task_notification_envelope(
            notification_id, kind=kind, status=status, summary=summary, body=body,
        )
        tmux_send_sync(tmux_session, envelope)
        _WAKE_IDS[key] = time.time()
        if len(_WAKE_IDS) > _WAKE_MAX:
            oldest = sorted(_WAKE_IDS, key=_WAKE_IDS.get)
            for stale in oldest[: len(_WAKE_IDS) - _WAKE_MAX]:
                _WAKE_IDS.pop(stale, None)
        return "accepted"
    except Exception:
        return "error"
