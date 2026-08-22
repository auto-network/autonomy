"""Personal-scoped capped log of client-side ceremony failures.

The factor and unlock ceremonies run their crypto in the browser (the root never
leaves the page — I1), so their exceptions never reach the server on their own;
they used to live only in a red banner an operator on a phone cannot inspect.
This append-only log captures them, bounded like the Agent Test telemetry so it
cannot grow without limit. It is DIAGNOSTIC ONLY: error descriptions and code
locations plus non-secret context (which transition, which factors were present)
— never a password, PRF output, root seed, CEK, or armor plaintext.
"""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    append_only_log,
    field,
    home,
    publication_band,
)

CLIENT_ERROR_SET_ID = "autonomy.identity.client-error"
CLIENT_ERROR_REVISION = 1

# Fields that are free text, with their maximum retained length.
_BOUNDS = (
    ("ceremony", 80),
    ("action", 80),
    ("name", 120),
    ("message", 600),
    ("stack", 4000),
    ("context", 600),
)


@home("personal")
@publication_band(max="raw")
@append_only_log
class ClientCeremonyErrorV1(SettingSchema):
    """One bounded client-side ceremony failure — diagnostic, never secrets."""

    set_id = CLIENT_ERROR_SET_ID
    schema_revision = CLIENT_ERROR_REVISION

    ceremony: str = field(required=True, description="Which ceremony failed (rearm, enroll, unlock, ...).")
    action: str = field(required=True, description="The specific action or step, or an empty string.")
    name: str = field(required=True, description="Error name/class, or an empty string.")
    message: str = field(required=True, description="Bounded error message; never secret material.")
    stack: str = field(required=True, description="Bounded stack / code locations, or an empty string.")
    context: str = field(required=True, description="Bounded non-secret JSON context (factors present, screen).")
    recorded_at: float = field(required=True, description="Unix timestamp the client reported the failure.")

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        for name, maximum in _BOUNDS:
            value = payload.get(name)
            if not isinstance(value, str) or len(value) > maximum:
                raise SchemaValidationError(f"{name} must be a bounded string")
        recorded_at = payload.get("recorded_at")
        if not isinstance(recorded_at, (int, float)) or isinstance(recorded_at, bool) \
                or recorded_at < 0 or recorded_at > 10 ** 11:
            raise SchemaValidationError("recorded_at must be a bounded timestamp")
