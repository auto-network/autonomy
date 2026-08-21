"""Setting schema for the auto-injected first turn in a fresh session.

The dashboard's ``api_session_create`` injects a primer first message
into every new interactive session (`server.py` around the existing
``_inject_first_message`` block). Historically this was a hardcoded
literal ``"Hello"``; this set turns it into a per-target,
operator-tunable template.

The set is ``dashboard.session.orientation`` keyed by launch target
(``@keyed_per_entity``). Workspace sessions use their workspace id. Native
host sessions use the reserved ``host`` key in the personal store. A row keyed
``__default__`` is an optional fallback; when no matching Setting exists, the
renderer uses the hardcoded defaults below. Per-target overrides take
precedence over ``__default__``.

The payload's ``template`` is a Jinja2 string rendered against:

- ``tmux_name``     — the freshly-allocated tmux session name
- ``workspace_id``  — workspace.id (empty string for the default
                       autonomy-session-platform container path)
- ``workspace_name``— workspace.name (or ``"default"``)
- ``ts``            — ISO 8601 UTC, seconds precision
- ``operator``      — empty string today (no host-side operator-
                       identity resolver exists yet; the variable is
                       exposed for forward compatibility, templates
                       referencing it MUST use ``{% if operator %}``
                       guards)

``enabled=false`` disables injection entirely for that key — the
session boots without an orientation turn.

Fresh and resumed sessions have separate templates because a resumed session
already carries its prior context and must not be re-oriented as a new one.
"""

from __future__ import annotations

from tools.graph.schemas.registry import (
    home,
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

DEFAULT_RESUME_TEMPLATE = (
    "Session {{tmux_name}} resumed at {{ts}}. Your prior context is intact — "
    "briefly confirm where things stand and that you're ready to continue."
)


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
@keyed_per_entity(key_strategy="workspace_id_or_session_kind")
class SessionOrientationV1(SettingSchema):
    """One row per launch target (plus the optional ``__default__``).

    The dashboard reads this set when a session is spawned and renders the
    resolved template into the first injected user turn. Workspace rows live
    in their owning org store; the native ``host`` row lives in ``personal``.
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
    resume_template: str = field(
        default=DEFAULT_RESUME_TEMPLATE,
        description=(
            "Jinja2 template for the first user turn injected after resuming "
            "an existing session. Variables match template. The default "
            "tells the agent its prior context is intact."
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
