"""Runtime probe for the ``autonomy/github`` capability.

A probe answers one question: *is this capability actually usable inside
the live workspace right now?* Static enablement (Setting rows in the
graph) is not enough — the live container may have lost ``gh``, the
auth env may not have flowed through, or the session may be dead.

The probe is deliberately tiny: it shells ``gh auth status`` inside the
target session's live container and maps the outcome to a normalized
result that Dashboard surfaces (and the eventual generic probe runner
in ``auto-hjr1d``) can render uniformly.

Result shape::

    {
        "contract": "source_control",
        "contract_version": 1,
        "implementation": "autonomy/github",
        "implementation_version": 1,
        "delivery_mode": "image_baked",
        "state": "ready" | "unavailable" | "degraded",
        "reason": None | "no_live_container" | "tool_missing"
                  | "env_missing" | "probe_failed",
        "missing_tools": [],
        "missing_env": [],
        "missing_secret_files": [],  # always [] for image_baked
        "details": {},
    }

``not_enabled`` is intentionally not produced here — it's a workspace-
level question that the probe runner answers before invoking us.
"""

from __future__ import annotations

# Import the service *module* (not its names) so test monkeypatches on
# ``service.run_cli`` / ``service.resolve_live_container`` reach the
# probe. ``from ... import run_cli`` would bind a stale reference at
# probe-import time and silently bypass test stubs.
from agents.capabilities.github import service

PROBE_COMMAND = ["gh", "auth", "status"]
DEFAULT_PROBE_TIMEOUT_SECONDS = 5

STATE_READY = "ready"
STATE_UNAVAILABLE = "unavailable"
STATE_DEGRADED = "degraded"

REASON_NO_LIVE_CONTAINER = "no_live_container"
REASON_TOOL_MISSING = "tool_missing"
REASON_ENV_MISSING = "env_missing"
REASON_PROBE_FAILED = "probe_failed"


def _result(
    *,
    state: str,
    reason: str | None = None,
    missing_tools: list[str] | None = None,
    missing_env: list[str] | None = None,
    details: dict | None = None,
) -> dict:
    return {
        "contract": "source_control",
        "contract_version": 1,
        "implementation": "autonomy/github",
        "implementation_version": 1,
        "delivery_mode": "image_baked",
        "state": state,
        "reason": reason,
        "missing_tools": list(missing_tools or []),
        "missing_env": list(missing_env or []),
        "missing_secret_files": [],
        "details": dict(details or {}),
    }


async def probe_v1(
    session_name: str,
    *,
    timeout: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> dict:
    """Probe ``autonomy/github`` against ``session_name``'s live container."""
    container = await service.resolve_live_container(session_name, timeout=timeout)
    if container is None:
        return _result(state=STATE_UNAVAILABLE, reason=REASON_NO_LIVE_CONTAINER)

    cmd = ["docker", "exec", container, *PROBE_COMMAND]
    stdout, stderr, exit_code, timed_out = await service.run_cli(cmd, timeout=timeout)
    failure = service.classify_failure(stdout, stderr, exit_code, timed_out)

    if failure is None:
        return _result(state=STATE_READY)
    if failure == service.FAILURE_GH_MISSING:
        return _result(
            state=STATE_DEGRADED,
            reason=REASON_TOOL_MISSING,
            missing_tools=["gh"],
        )
    if failure == service.FAILURE_AUTH_MISSING:
        return _result(
            state=STATE_DEGRADED,
            reason=REASON_ENV_MISSING,
            missing_env=["GH_TOKEN"],
        )
    return _result(
        state=STATE_DEGRADED,
        reason=REASON_PROBE_FAILED,
        details={
            "exit_code": exit_code,
            "stderr": (stderr or "").strip()[:500],
            "timed_out": timed_out,
        },
    )


__all__ = [
    "PROBE_COMMAND",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "STATE_READY",
    "STATE_UNAVAILABLE",
    "STATE_DEGRADED",
    "REASON_NO_LIVE_CONTAINER",
    "REASON_TOOL_MISSING",
    "REASON_ENV_MISSING",
    "REASON_PROBE_FAILED",
    "probe_v1",
]
