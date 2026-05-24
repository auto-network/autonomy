"""Render the auto-injected first turn from the orientation Setting.

Wraps the ``dashboard.session.orientation`` lookup + Jinja2 render so
the call site in ``api_session_create`` stays a one-liner.

The set is per-workspace keyed (``@keyed_per_entity``); a ``__default__``
row is the fallback when no workspace-specific override exists. If the
resolved row's ``enabled`` is False, this function returns ``None`` and
the caller skips the inject-first-message scheduling entirely.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateError

from tools.dashboard.session_orientation_settings import (
    DEFAULT_KEY,
    DEFAULT_TEMPLATE,
    SCHEMA_REVISION,
    SESSION_ORIENTATION_SET_ID,
)


logger = logging.getLogger(__name__)


_JINJA_ENV = Environment(undefined=StrictUndefined, autoescape=False)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_payload(workspace_id: str, *, org: Any) -> dict | None:
    """Resolve the orientation payload for *workspace_id*, falling back to default.

    Returns the resolved payload dict (with ``template`` and ``enabled``
    keys) or ``None`` if neither the per-workspace nor the default row
    can be read. The caller treats ``None`` as "use the hardcoded
    default template" so a missing Settings backend never breaks
    session creation.
    """
    from tools.graph import settings_ops

    try:
        members = settings_ops.read_set(
            SESSION_ORIENTATION_SET_ID,
            org=org,
            peers=[],
            target_revision=SCHEMA_REVISION,
        )
    except Exception:
        logger.warning(
            "session_orientation: read_set(%s) failed",
            SESSION_ORIENTATION_SET_ID,
            exc_info=True,
        )
        return None

    by_key: dict[str, dict] = {}
    for member in members.members:
        if isinstance(member.payload, dict):
            by_key[member.key or ""] = dict(member.payload)

    if workspace_id and workspace_id in by_key:
        return by_key[workspace_id]
    return by_key.get(DEFAULT_KEY)


def render_orientation(
    *,
    tmux_name: str,
    workspace_id: str,
    workspace_name: str,
    org: Any,
    operator: str = "",
) -> str | None:
    """Render the orientation message for a freshly-created session.

    Returns:
      - the rendered text, ready for ``tmux_send`` injection
      - ``None`` when orientation is disabled for this workspace
        (``payload.enabled is False``) — caller skips injection entirely

    Resolution falls back through, in order:
      1. ``dashboard.session.orientation:{workspace_id}`` (per-workspace)
      2. ``dashboard.session.orientation:__default__`` (global)
      3. The hardcoded ``DEFAULT_TEMPLATE`` (Settings backend unreachable)

    A template that raises a Jinja error logs a warning and falls
    through to the hardcoded default — a malformed operator-tuned
    template can never break session creation.
    """
    payload = _read_payload(workspace_id, org=org) or {}
    enabled = payload.get("enabled", True)
    if not enabled:
        return None

    template_src = payload.get("template") or DEFAULT_TEMPLATE
    context = {
        "tmux_name": tmux_name,
        "workspace_id": workspace_id or "",
        "workspace_name": workspace_name or "default",
        "ts": _iso_now(),
        "operator": operator or "",
    }
    try:
        rendered = _JINJA_ENV.from_string(template_src).render(**context)
    except TemplateError:
        logger.warning(
            "session_orientation: template render failed for workspace=%s; "
            "falling back to default",
            workspace_id, exc_info=True,
        )
        rendered = _JINJA_ENV.from_string(DEFAULT_TEMPLATE).render(**context)

    rendered = rendered.strip()
    return rendered or None
