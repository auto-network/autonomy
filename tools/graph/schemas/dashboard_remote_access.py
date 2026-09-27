"""How the operator reaches this dashboard from elsewhere (graph://c9d72ea4-feb §10).

One row per operator, ``personal``-homed so every machine of the fleet sees
the same answer: which reach mode onboarding chose (the Autonomy Network relay,
Tailscale, or local only) and the origin every link to this dashboard should
use (auto-w622e). For the relay mode the row also names the reservation the
dashboard is published under. The gate section (the passkey helper's
enrollment state) is added by the helper's own bead; nothing here is a secret.
"""

from __future__ import annotations

import re
from typing import Any

from .namespace_reservation import _is_rfc3339_millis, validate_reservation_key
from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    publication_band,
    singleton,
)

REMOTE_ACCESS_SET_ID = "autonomy.dashboard.remote-access"
REMOTE_ACCESS_REVISION = 1
REMOTE_ACCESS_KEY = "default"

REACH_MODES = ("autonomy", "tailscale", "local")

_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?$")
_APP_LABEL_RE = re.compile(r"^(?![a-z0-9]{2}--)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@home("personal")
@publication_band(max="raw")
@singleton(key=REMOTE_ACCESS_KEY)
class DashboardRemoteAccessV1(SettingSchema):
    """The reach mode onboarding recorded and the origin links must use."""

    set_id = REMOTE_ACCESS_SET_ID
    schema_revision = REMOTE_ACCESS_REVISION

    mode: str = field(required=True, description="autonomy (relay), tailscale, or local.")
    origin: str = field(
        required=True,
        description="The dashboard's origin for every link: the relay origin, the Tailnet "
                    "origin, or the local address onboarding ran on.",
    )
    reservation_id: str = field(required=False, description="Relay mode: the Service reservation the dashboard is published under.")
    app_label: str = field(required=False, description="Relay mode: the app label of that reservation.")
    published_at: str = field(required=True, description="When this mode was recorded (UTC RFC 3339, milliseconds).")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        mode = payload.get("mode")
        if mode not in REACH_MODES:
            raise SchemaValidationError(f"{cls.__name__}: mode must be one of {', '.join(REACH_MODES)}")
        origin = payload.get("origin")
        if not isinstance(origin, str) or not _ORIGIN_RE.fullmatch(origin):
            raise SchemaValidationError(f"{cls.__name__}: origin must be an http(s) origin with no path")
        if mode == "autonomy":
            if not origin.startswith("https://"):
                raise SchemaValidationError(f"{cls.__name__}: a relay origin is https")
            validate_reservation_key(payload.get("reservation_id"))
            app_label = payload.get("app_label")
            if not isinstance(app_label, str) or not _APP_LABEL_RE.fullmatch(app_label):
                raise SchemaValidationError(f"{cls.__name__}: app_label must be a lowercase DNS label")
        else:
            for name in ("reservation_id", "app_label"):
                if name in payload:
                    raise SchemaValidationError(f"{cls.__name__}: {name} belongs to the relay mode only")
        if not _is_rfc3339_millis(payload.get("published_at")):
            raise SchemaValidationError(f"{cls.__name__}: published_at must be UTC RFC 3339 with milliseconds")
