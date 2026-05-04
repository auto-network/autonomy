"""Settings contract + normalization helpers for harness usage tiles."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    cache,
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
    "related_set_ids": [],
}


_VALID_HARNESSES = {"claude", "codex"}
_VALID_STATUSES = {"ok", "unavailable"}
_OPTIONAL_STRINGS = {
    "account_id",
    "plan_type",
    "tier",
    "limit_id",
    "limit_name",
    "rate_limit_reached_type",
    "note",
}
_WINDOW_KEYS = {"short", "long"}


@cache(ttl=HARNESS_USAGE_CACHE_TTL)
class DashboardHarnessUsageV1(SettingSchema):
    """Per-auth-identity harness usage row."""

    set_id = HARNESS_USAGE_SET_ID
    schema_revision = HARNESS_USAGE_SCHEMA_REVISION

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )

        required = {
            "harness",
            "identity_id",
            "identity_label",
            "status",
            "source",
            "updated_at",
            "windows",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise SchemaValidationError(
                f"{cls.__name__}: missing required field(s): {missing}"
            )

        harness = payload.get("harness")
        if harness not in _VALID_HARNESSES:
            raise SchemaValidationError(
                f"{cls.__name__}: harness must be one of "
                f"{sorted(_VALID_HARNESSES)}, got {harness!r}"
            )

        for key in ("identity_id", "identity_label", "source", "updated_at"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a non-empty string"
                )

        status = payload.get("status")
        if status not in _VALID_STATUSES:
            raise SchemaValidationError(
                f"{cls.__name__}: status must be one of "
                f"{sorted(_VALID_STATUSES)}, got {status!r}"
            )

        windows = payload.get("windows")
        if not isinstance(windows, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: 'windows' must be a dict"
            )
        extra_windows = sorted(set(windows) - _WINDOW_KEYS)
        if extra_windows:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown window key(s): {extra_windows}"
            )
        for window_name, window in windows.items():
            _validate_window(cls.__name__, window_name, window)

        for key in _OPTIONAL_STRINGS:
            if key in payload and payload[key] is not None \
                    and not isinstance(payload[key], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a string or null"
                )

        extra = set(payload) - (
            required | _OPTIONAL_STRINGS
        )
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


def fingerprint_secret(secret: str) -> str:
    """Stable, non-reversible short fingerprint for credential identity."""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


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


def load_claude_credential_bundle(path: str | Path) -> dict[str, Any] | None:
    """Load a Claude OAuth credential bundle from a copied run-dir file."""

    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None

    if p.name == ".setup-token":
        token = raw.strip()
        if not token:
            return None
        return {
            "source_kind": "setup_token",
            "path": str(p),
            "access_token": token,
            "refresh_token": None,
            "fingerprint": fingerprint_secret(token),
            "subscription_type": None,
            "rate_limit_tier": None,
            "expires_at_ms": None,
        }

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token.strip():
        return None
    refresh_token = oauth.get("refreshToken")
    expires_at_ms = oauth.get("expiresAt")
    try:
        expires_at_ms = int(expires_at_ms) if expires_at_ms is not None else None
    except (TypeError, ValueError):
        expires_at_ms = None

    fingerprint_basis = refresh_token if isinstance(refresh_token, str) and refresh_token else access_token
    return {
        "source_kind": "credentials_json",
        "path": str(p),
        "access_token": access_token,
        "refresh_token": refresh_token if isinstance(refresh_token, str) and refresh_token else None,
        "fingerprint": fingerprint_secret(fingerprint_basis),
        "subscription_type": oauth.get("subscriptionType")
        if isinstance(oauth.get("subscriptionType"), str) else None,
        "rate_limit_tier": oauth.get("rateLimitTier")
        if isinstance(oauth.get("rateLimitTier"), str) else None,
        "expires_at_ms": expires_at_ms,
    }


def candidate_claude_credential_paths(row: dict[str, Any]) -> list[Path]:
    """Best-effort credential locations for a live Claude session row."""

    roots: list[Path] = []
    resolution_dir = row.get("resolution_dir")
    if isinstance(resolution_dir, str) and resolution_dir.strip():
        roots.append(Path(resolution_dir))

    jsonl_path = row.get("jsonl_path")
    if isinstance(jsonl_path, str) and jsonl_path.strip():
        roots.append(Path(jsonl_path).parent)

    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        for parent in _walk_roots(root):
            for name in (".credentials.json", ".setup-token"):
                candidate = parent / name
                marker = str(candidate)
                if marker in seen:
                    continue
                seen.add(marker)
                out.append(candidate)
    return out


def _walk_roots(root: Path) -> Iterable[Path]:
    current = root
    for _ in range(4):
        yield current
        if current.parent == current:
            break
        current = current.parent


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
) -> dict[str, Any]:
    identity = _resolve_claude_identity(bundle, org_id)
    return _build_usage_payload(
        harness="claude",
        identity_id=identity["identity_id"],
        identity_label=identity["identity_label"],
        account_id=identity["account_id"],
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
) -> dict[str, Any]:
    return _build_usage_payload(
        harness=harness,
        identity_id=identity_id,
        identity_label=identity_label,
        account_id=account_id,
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


def _resolve_claude_identity(
    bundle: dict[str, Any],
    org_id: str | None,
) -> dict[str, str | None]:
    if org_id:
        return {
            "identity_id": f"org:{org_id}",
            "identity_label": short_identity_label("org", org_id),
            "account_id": org_id,
        }
    fingerprint = str(bundle["fingerprint"])
    return {
        "identity_id": f"acct:{fingerprint}",
        "identity_label": short_identity_label("acct", fingerprint),
        "account_id": None,
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
