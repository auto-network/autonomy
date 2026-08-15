"""Setting schema for the auto-injected first turn in a fresh session.

The dashboard's ``api_session_create`` injects a primer first message
into every new workspace session (`server.py` around the existing
``_inject_first_message`` block). Historically this was a hardcoded
literal ``"Hello"``; this set turns it into a per-workspace,
operator-tunable template.

The set is ``dashboard.session.orientation`` keyed per workspace
(``@keyed_per_entity``). A row keyed ``__default__`` is the global
fallback, seeded at first import. Per-workspace overrides take
precedence.

The payload's ``template`` is a Jinja2 string rendered against:

- ``tmux_name``     — the freshly-allocated tmux session name
- ``workspace_id``  — workspace.id (empty string for the default
                       autonomy-agent:dashboard container path)
- ``workspace_name``— workspace.name (or ``"default"``)
- ``ts``            — ISO 8601 UTC, seconds precision
- ``operator``      — empty string today (no host-side operator-
                       identity resolver exists yet; the variable is
                       exposed for forward compatibility, templates
                       referencing it MUST use ``{% if operator %}``
                       guards)

``enabled=false`` disables injection entirely for that key — the
session boots without an orientation turn.
"""

from __future__ import annotations

from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    keyed_per_entity,
)


SESSION_ORIENTATION_SET_ID = "dashboard.session.orientation"
SCHEMA_REVISION = 1

DEFAULT_KEY = "__default__"

DEFAULT_TEMPLATE = (
    "Session {{tmux_name}} started in workspace "
    "{{workspace_name}} at {{ts}}."
)


@keyed_per_entity(key_strategy="session_name")
class SessionOrientationV1(SettingSchema):
    """One row per workspace (plus the global ``__default__``).

    The dashboard reads this set at the moment a new container session
    is being spawned and renders the resolved template into the first
    injected user turn.
    """

    set_id = SESSION_ORIENTATION_SET_ID
    schema_revision = SCHEMA_REVISION

    template: str = field(
        default=DEFAULT_TEMPLATE,
        description=(
            "Jinja2 template for the first auto-injected user turn. "
            "Variables: tmux_name, workspace_id, workspace_name, ts, "
            "operator. Operator is currently always empty — templates "
            "referencing it should use {% if operator %} guards."
        ),
    )
    enabled: bool = field(
        default=True,
        description=(
            "When false, no first turn is auto-injected for this key. "
            "Primer-URL-driven sessions are unaffected (primer always "
            "takes precedence over orientation)."
        ),
    )
