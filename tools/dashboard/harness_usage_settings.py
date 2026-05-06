"""Settings contract + normalization helpers for harness usage tiles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    cache,
    field,
    keyed_per_entity,
)


HARNESS_USAGE_SET_ID = "dashboard.harness.usage"
HARNESS_USAGE_SCHEMA_REVISION = 1

# The background publisher refreshes active identities every minute.
# Keep UI staleness much tighter than the cache TTL, and let cache-gc
# sweep old rows eventually when identities stop reporting entirely.
HARNESS_USAGE_CACHE_TTL = timedelta(minutes=15)


SYNOPSIS = {
    "summary": (
        "Dashboard footer harness-usage cache. One row per harness auth "
        "identity carrying compact rate-limit telemetry for Codex and Claude."
    ),
    "nouns": [
        "harness usage",
        "rate limits",
        "claude usage",
        "codex usage",
        "footer tiles",
        "auth identity",
    ],
    "related_set_ids": [
        "dashboard.claude.credentials#1",
    ],
}


_VALID_HARNESSES = ("claude", "codex")
_VALID_STATUSES = ("ok", "unavailable")
_VALID_SOURCES = ("oauth_usage", "transcript")


@cache(ttl=HARNESS_USAGE_CACHE_TTL)
@keyed_per_entity
class DashboardHarnessUsageV1(SettingSchema):
    """Per-auth-identity harness usage row.

    Auto-10lsv migrated this schema from imperative ``validate()`` checks
    to declarative typed-field annotations. Field metadata now drives
    ``graph set schema`` introspection, ``graph set example`` stub
    payloads, and the substrate's unknown-field check; ``validate()``
    keeps a slim residual check for the per-window substructure that
    typed annotations can't express.
    """

    set_id = HARNESS_USAGE_SET_ID
    schema_revision = HARNESS_USAGE_SCHEMA_REVISION

    harness: str = field(
        required=True,
        enum=list(_VALID_HARNESSES),
        description="Harness this row reports for.",
    )
    identity_id: str = field(
        required=True,
        description="Stable per-account id (e.g. 'org:<uuid>').",
    )
    identity_label: str = field(
        required=True,
        description="Auto-derived short label for footer rendering.",
    )
    alias: str | None = field(
        default=None,
        description=(
            "Operator-controlled friendly alias (e.g. 'primary'). For Claude "
            "this is the ``alias`` field on the matching "
            "``dashboard.claude.credentials`` row; informational on this row "
            "so the dashboard can render the friendly name without a join. "
            "None for non-token paths (transcript, env override)."
        ),
    )
    account_id: str | None = field(
        default=None,
        description="Anthropic org UUID (Claude only).",
    )
    status: str = field(
        required=True,
        enum=list(_VALID_STATUSES),
        description="'ok' when telemetry is fresh; 'unavailable' otherwise.",
    )
    source: str = field(
        required=True,
        enum=list(_VALID_SOURCES),
        description="Data origin: 'oauth_usage' for Claude /usage; 'transcript' for Codex.",
    )
    updated_at: str = field(
        required=True,
        description="ISO-8601 timestamp the publisher stamped at write time.",
    )
    plan_type: str | None = field(default=None)
    tier: str | None = field(default=None)
    limit_id: str | None = field(default=None)
    limit_name: str | None = field(default=None)
    rate_limit_reached_type: str | None = field(default=None)
    note: str | None = field(default=None)
    windows: dict = field(
        default_factory=dict,
        description=(
            "{short, long} → {used_percent, window_minutes, resets_at}. "
            "Empty dict when status='unavailable'."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        """Substructure validator for the typed-field-derived schema.

        Typed annotations cover field types, required-ness, enums, and
        unknown-field rejection (via the ``_field_metadata`` set used
        below). The window dict still needs hand-rolled substructure
        checks because the declarative form can't express element shape
        for nested dicts — kept here as the smallest possible residual
        override.
        """
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )

        for required_field, meta in cls._field_metadata.items():
            if not meta.get("required"):
                continue
            value = payload.get(required_field)
            if value is None:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing required field "
                    f"{required_field!r}"
                )
            if meta.get("type") == "string" and (
                not isinstance(value, str) or not value.strip()
            ):
                raise SchemaValidationError(
                    f"{cls.__name__}: {required_field!r} must be a "
                    f"non-empty string"
                )

        for enum_field, meta in cls._field_metadata.items():
            allowed = meta.get("enum")
            if not allowed:
                continue
            if enum_field not in payload:
                continue
            value = payload[enum_field]
            if value is None and not meta.get("required"):
                continue
            if value not in allowed:
                raise SchemaValidationError(
                    f"{cls.__name__}: {enum_field!r} must be one of "
                    f"{list(allowed)}, got {value!r}"
                )

        # Optional string-typed fields may be string OR null.
        for opt_field, meta in cls._field_metadata.items():
            if meta.get("required"):
                continue
            if meta.get("type") != "string":
                continue
            if opt_field not in payload:
                continue
            value = payload[opt_field]
            if value is not None and not isinstance(value, str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {opt_field!r} must be a string or null"
                )

        windows = payload.get("windows")
        if windows is None:
            windows = {}
        if not isinstance(windows, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: 'windows' must be a dict"
            )
        extra_windows = sorted(set(windows) - {"short", "long"})
        if extra_windows:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown window key(s): {extra_windows}"
            )
        for window_name, window in windows.items():
            _validate_window(cls.__name__, window_name, window)

        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


def _validate_window(schema_name: str, window_name: str, window: Any) -> None:
    if not isinstance(window, dict):
        raise SchemaValidationError(
            f"{schema_name}: window {window_name!r} must be a dict"
        )
    allowed = {"used_percent", "window_minutes", "resets_at"}
    extra = sorted(set(window) - allowed)
    if extra:
        raise SchemaValidationError(
            f"{schema_name}: window {window_name!r} has unknown field(s): {extra}"
        )
    used = window.get("used_percent")
    if used is not None and not isinstance(used, (int, float)):
        raise SchemaValidationError(
            f"{schema_name}: window {window_name!r} used_percent must be numeric or null"
        )
    minutes = window.get("window_minutes")
    if minutes is not None and not isinstance(minutes, int):
        raise SchemaValidationError(
            f"{schema_name}: window {window_name!r} window_minutes must be int or null"
        )
    resets_at = window.get("resets_at")
    if resets_at is not None and not isinstance(resets_at, int):
        raise SchemaValidationError(
            f"{schema_name}: window {window_name!r} resets_at must be int or null"
        )


def make_harness_usage_key(harness: str, identity_id: str) -> str:
    return f"{(harness or '').strip().lower()}:{(identity_id or '').strip()}"


def short_identity_label(prefix: str, raw_id: str) -> str:
    trimmed = (raw_id or "").strip()
    if not trimmed:
        return prefix
    if len(trimmed) <= 8:
        return f"{prefix} {trimmed}"
    return f"{prefix} {trimmed[:8]}"


def iso_to_epoch_seconds(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp())


def normalize_codex_usage_payload(
    state: dict[str, Any],
    *,
    identity_id: str = "default",
    identity_label: str = "default",
    updated_at: str | None = None,
) -> dict[str, Any]:
    windows = state.get("windows") if isinstance(state.get("windows"), dict) else {}
    return _build_usage_payload(
        harness="codex",
        identity_id=identity_id,
        identity_label=identity_label,
        status="ok",
        source=_string_or(state.get("source"), "transcript"),
        updated_at=(
            updated_at
            or _string_or(state.get("updated_at"))
            or datetime.now(timezone.utc).isoformat()
        ),
        plan_type=_string_or(state.get("plan_type")),
        limit_id=_string_or(state.get("limit_id")),
        limit_name=_string_or(state.get("limit_name")),
        rate_limit_reached_type=_string_or(state.get("rate_limit_reached_type")),
        windows=_normalize_existing_windows(windows),
    )


def normalize_claude_usage_payload(
    *,
    bundle: dict[str, Any],
    usage_body: dict[str, Any],
    org_id: str | None,
    updated_at: str,
    alias: str | None = None,
) -> dict[str, Any]:
    """Build a Claude harness-usage row payload.

    ``alias`` is the operator-facing friendly name from the matching
    ``dashboard.claude.credentials`` row (the same field operators set
    via ``graph claude install --alias <name>``). Stamped onto every
    row so the dashboard can render the friendly name without a join.
    """
    identity = _resolve_claude_identity(org_id)
    return _build_usage_payload(
        harness="claude",
        identity_id=identity["identity_id"],
        identity_label=identity["identity_label"],
        account_id=identity["account_id"],
        alias=alias,
        status="ok",
        source="oauth_usage",
        updated_at=updated_at,
        plan_type=_string_or(bundle.get("subscription_type")),
        tier=_string_or(bundle.get("rate_limit_tier")),
        windows=_build_windows({
            "short": _normalize_claude_usage_window(usage_body.get("five_hour"), 300),
            "long": _normalize_claude_usage_window(usage_body.get("seven_day"), 10080),
        }),
    )


def make_unavailable_usage_payload(
    *,
    harness: str,
    identity_id: str,
    identity_label: str,
    source: str,
    note: str,
    updated_at: str,
    account_id: str | None = None,
    plan_type: str | None = None,
    tier: str | None = None,
    alias: str | None = None,
) -> dict[str, Any]:
    return _build_usage_payload(
        harness=harness,
        identity_id=identity_id,
        identity_label=identity_label,
        account_id=account_id,
        alias=alias,
        status="unavailable",
        source=source,
        updated_at=updated_at,
        plan_type=plan_type,
        tier=tier,
        note=note,
        windows={},
    )


def _build_usage_payload(
    *,
    harness: str,
    identity_id: str,
    identity_label: str,
    status: str,
    source: str,
    updated_at: str,
    windows: dict[str, dict[str, int | float | None]],
    account_id: str | None = None,
    alias: str | None = None,
    plan_type: str | None = None,
    tier: str | None = None,
    limit_id: str | None = None,
    limit_name: str | None = None,
    rate_limit_reached_type: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "harness": harness,
        "identity_id": identity_id,
        "identity_label": identity_label,
        "alias": alias,
        "account_id": account_id,
        "status": status,
        "source": source,
        "updated_at": updated_at,
        "plan_type": plan_type,
        "tier": tier,
        "limit_id": limit_id,
        "limit_name": limit_name,
        "rate_limit_reached_type": rate_limit_reached_type,
        "note": note,
        "windows": windows,
    }


def _resolve_claude_identity(org_id: str | None) -> dict[str, str | None]:
    """Build identity fields for a Claude row.

    Pre-auto-10lsv this had a fingerprint fallback for the
    ``acct:<fp>`` case. After 28a1b56 (zombie-row cleanup) we never
    write acct rows — every successful poll has an org id from the
    /usage response header, and unsuccessful polls write nothing.
    Callers that want a placeholder row (no live token) use
    :func:`make_unavailable_usage_payload` directly with
    ``identity_id="unresolved"``.
    """
    if not org_id:
        raise ValueError(
            "Claude harness usage requires an org_id; the /usage response "
            "header carries one on every successful poll."
        )
    return {
        "identity_id": f"org:{org_id}",
        "identity_label": short_identity_label("org", org_id),
        "account_id": org_id,
    }


def _build_windows(
    windows: dict[str, dict[str, int | float | None] | None],
) -> dict[str, dict[str, int | float | None]]:
    return {
        name: window
        for name, window in windows.items()
        if isinstance(window, dict)
    }


def _normalize_existing_windows(
    windows: dict[str, Any],
) -> dict[str, dict[str, int | float | None]]:
    out: dict[str, dict[str, int | float | None]] = {}
    for name in ("short", "long"):
        window = windows.get(name)
        if not isinstance(window, dict):
            continue
        out[name] = {
            "used_percent": _coerce_float(window.get("used_percent")),
            "window_minutes": _coerce_int(window.get("window_minutes")),
            "resets_at": _coerce_int(window.get("resets_at")),
        }
    return out


def _string_or(value: Any, default: str | None = None) -> str | None:
    return value if isinstance(value, str) else default


def _normalize_claude_usage_window(
    window: Any,
    window_minutes: int,
) -> dict[str, int | float | None] | None:
    if not isinstance(window, dict):
        return None
    utilization = _coerce_float(window.get("utilization"))
    resets_at = iso_to_epoch_seconds(window.get("resets_at"))
    if utilization is None and resets_at is None:
        return None
    return {
        "used_percent": utilization,
        "window_minutes": window_minutes,
        "resets_at": resets_at,
    }


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
