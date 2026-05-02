"""Mediator action: deliver SurfacePing rows over CrossTalk (substrate.C).

Bead ``auto-9gxo8``. Third of the substrate-presence wedge — wires the
:class:`tools.graph.surface.SurfacePingV1` schema (substrate.A) to the
existing CrossTalk delivery channel via the settings-mediator substrate
(:mod:`tools.dashboard.settings_mediator`).

The handler routes by the row's explicit ``to_participant_id`` — never
by role lookup. See ``graph://1ba4d2e0-c5f`` for the role-filter
pitfall, and ``graph://dff97eec-c59`` for the broader signpost.

Module is imported eagerly from ``server.py`` so the registration runs
before the action loop starts ticking.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from tools.dashboard.settings_mediator import register_action_decorator
from tools.graph.surface import SURFACE_PING_SET_ID


# ── CrosstalkService ────────────────────────────────────────────────


class CrosstalkService:
    """Substrate-side CrossTalk sender wired into :class:`Services`.

    Wraps :func:`tools.dashboard.tmux_send.tmux_send` with envelope
    construction. Unlike :func:`tools.dashboard.server.api_crosstalk_send`,
    this path is internal — sender identity comes from the originating
    Setting row (``from`` attribute), not from a CROSSTALK_TOKEN, and no
    auth.db row is written. ``tmux_send`` is fire-and-forget and never
    raises on a missing target session, so a ping aimed at a session
    that isn't currently live degrades to a no-op delivery — the row
    persists in the SurfacePing log either way.
    """

    def __init__(
        self,
        send_fn: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self._send = send_fn

    async def send(
        self,
        *,
        target: str,
        kind: str,
        body: dict,
    ) -> None:
        """Deliver a structured CrossTalk envelope to *target*.

        ``body`` is the structured envelope payload produced by a
        formatter like :func:`format_ping`:

        * ``body["from"]`` — required; the sender attribute baked into
          the envelope's ``from="..."`` header.
        * ``body["attrs"]`` — optional dict of additional envelope
          attributes (e.g. ``surface``, ``position``, ``message``);
          rendered in declaration order after ``from`` and ``kind``.
        * ``body["text"]`` — the inner text content of the envelope
          (between the open and close tags).
        """
        from_id = body["from"]
        text = body.get("text", "")
        extra = body.get("attrs", {}) or {}
        envelope = build_envelope(
            from_id=from_id, kind=kind, extra=extra, body=text,
        )
        await self._send(target, envelope)


# ── Envelope construction ───────────────────────────────────────────


def _escape(value: object) -> str:
    """Escape a string for inclusion as a double-quoted XML attribute."""
    s = "" if value is None else str(value)
    return (
        s.replace("&", "&amp;")
         .replace('"', "&quot;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def build_envelope(
    *,
    from_id: str,
    kind: str,
    extra: dict[str, object],
    body: str,
) -> str:
    """Render a multi-line ``<crosstalk ...>...</crosstalk>`` envelope.

    Layout matches the existing ``api_crosstalk_send`` envelope —
    first attribute on the same line as the tag name, subsequent
    attributes indented eleven spaces — so receiving sessions can
    apply the same parser to substrate-issued and operator-issued
    messages alike.
    """
    lines = [f'<crosstalk from="{_escape(from_id)}"']
    lines.append(f'           kind="{_escape(kind)}"')
    for k, v in extra.items():
        lines.append(f'           {k}="{_escape(v)}"')
    open_tag = "\n".join(lines) + ">"
    return f"{open_tag}\n{body}\n</crosstalk>"


# ── Ping formatter ──────────────────────────────────────────────────


def format_ping(payload: dict) -> dict:
    """Build the structured CrossTalk body for a SurfacePing row.

    The recipient session sees a multi-attribute envelope identifying
    the sender, the surface, the position they're being summoned to,
    and any free-text message — plus a short prose body explaining
    what happened in case the recipient parser ignores attributes.
    """
    surface = payload["surface_id"]
    position = (
        f"{payload['position_kind']}:{payload['position_value']}"
    )
    return {
        "from": payload["from_participant_id"],
        "attrs": {
            "surface": surface,
            "position": position,
            "message": payload.get("message", ""),
        },
        "text": f"You were summoned to {surface}.",
    }


# ── Mediator action ─────────────────────────────────────────────────


@register_action_decorator(
    SURFACE_PING_SET_ID,
    name="surface.ping.deliver",
)
async def deliver_ping(row, services) -> None:
    """Deliver a SurfacePing row via CrossTalk to its explicit target.

    Routing by ``to_participant_id`` is non-negotiable: substituting a
    role lookup here would silently drop pings whenever the lookup
    misses (multiple role-holders, stale rows, etc.). See
    ``graph://1ba4d2e0-c5f``.
    """
    payload = row.payload
    await services.crosstalk.send(
        target=payload["to_participant_id"],
        kind="surface-ping",
        body=format_ping(payload),
    )
