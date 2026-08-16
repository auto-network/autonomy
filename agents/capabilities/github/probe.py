"""Runtime probe for the ``autonomy/github`` capability.

A probe answers one question: *is this capability actually usable inside
the live workspace right now?* Static enablement (Setting rows in the
graph) is not enough — the live container may have lost ``gh``, the
auth env may not have flowed through, or the session may be dead.

The probe is deliberately tiny: it shells ``gh auth status`` inside the
target session's live container and maps the outcome to a normalized
:class:`ProbeResult` that Dashboard surfaces (and the eventual generic
probe runner in ``auto-hjr1d``) can render uniformly. ``.to_dict()`` is
the JSON-boundary serializer; the dataclass declaration is the
canonical shape ahead of auto-0425-010430's typed-fields substrate.

``not_enabled`` is intentionally not produced here — it's a workspace-
level question that the probe runner answers before invoking us.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class ProbeResult:
    """Normalized capability-probe outcome for one workspace session.

    The runner in ``auto-hjr1d`` will surface this shape uniformly across
    every capability, so Dashboard / Worktrees can answer "ready,
    unavailable, or degraded — and why" without provider-specific
    branching. Defaults match a successful ``autonomy/github`` probe so
    the helpers below only have to set the fields that diverge.
    """

    contract: str = "source_control"
    contract_version: int = 1
    implementation: str = "autonomy/github"
    implementation_version: int = 1
    delivery_mode: str = "image_baked"
    state: str = STATE_READY
    reason: str | None = None
    missing_tools: tuple[str, ...] = ()
    missing_env: tuple[str, ...] = ()
    missing_secret_files: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "contract": self.contract,
            "contract_version": self.contract_version,
            "implementation": self.implementation,
            "implementation_version": self.implementation_version,
            "delivery_mode": self.delivery_mode,
            "state": self.state,
            "reason": self.reason,
            "missing_tools": list(self.missing_tools),
            "missing_env": list(self.missing_env),
            "missing_secret_files": list(self.missing_secret_files),
            "details": dict(self.details),
        }


# Wire the runtime dataclass to its declarative typed schema. The link
# is checked at import time — any drift between the dataclass's field
# set and the schema's declared metadata raises immediately, so the
# generated `capability-github.d.ts` consumed by worktrees.js can't
# claim a probe field the dataclass doesn't actually carry (Bead 3D).
from agents.capabilities.github.schemas import (  # noqa: E402 — defined after dataclass
    ProbeResultV1,
    link_dataclass_to_schema as _link_payload_schema,
)

_link_payload_schema(ProbeResult, ProbeResultV1)


async def probe_v1(
    session_name: str,
    *,
    timeout: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Probe ``autonomy/github`` against ``session_name``'s live container."""
    container = await service.resolve_live_container(session_name, timeout=timeout)
    if container is None:
        return ProbeResult(state=STATE_UNAVAILABLE, reason=REASON_NO_LIVE_CONTAINER)

    cmd = ["docker", "exec", container, *PROBE_COMMAND]
    stdout, stderr, exit_code, timed_out = await service.run_cli(cmd, timeout=timeout)
    failure = service.classify_failure(stdout, stderr, exit_code, timed_out)

    if failure is None:
        return ProbeResult(state=STATE_READY)
    if failure == service.FAILURE_GH_MISSING:
        return ProbeResult(
            state=STATE_DEGRADED,
            reason=REASON_TOOL_MISSING,
            missing_tools=("gh",),
        )
    if failure == service.FAILURE_AUTH_MISSING:
        return ProbeResult(
            state=STATE_DEGRADED,
            reason=REASON_ENV_MISSING,
            missing_env=("GH_TOKEN",),
        )
    return ProbeResult(
        state=STATE_DEGRADED,
        reason=REASON_PROBE_FAILED,
        details={
            "exit_code": exit_code,
            "stderr": (stderr or "").strip()[:500],
            "timed_out": timed_out,
        },
    )


async def probe_host_v1(
    host: str,
    *,
    timeout: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Probe host-mode execution for ``host`` (e.g. ``github.com``).

    READY means the dashboard can run ``gh`` directly with the token file
    configured for that git host (graph note f7c4c109-91a §Phase 1a) — no
    agent container involved. UNAVAILABLE = no token configured; DEGRADED
    mirrors the container probe's classification.

    Takes no org: this asks whether host mode works for a git host at all.
    Where exactly one organization has configured the host that is
    unambiguous, and where several have, the answer depends on which one
    is asking and the caller has to say.
    """
    import os

    token = service.github_host_token(host)
    if token is None:
        return ProbeResult(
            state=STATE_UNAVAILABLE,
            reason=REASON_ENV_MISSING,
            missing_env=("autonomy.credential-file <org>:" + host,),
        )
    stdout, stderr, exit_code, timed_out = await service.run_cli(
        list(PROBE_COMMAND),
        timeout=timeout,
        env={**os.environ, "GH_TOKEN": token, "GH_PROMPT_DISABLED": "1"},
    )
    failure = service.classify_failure(stdout, stderr, exit_code, timed_out)
    if failure is None:
        return ProbeResult(state=STATE_READY)
    if failure == service.FAILURE_GH_MISSING:
        return ProbeResult(
            state=STATE_DEGRADED,
            reason=REASON_TOOL_MISSING,
            missing_tools=("gh",),
        )
    if failure == service.FAILURE_AUTH_MISSING:
        return ProbeResult(
            state=STATE_DEGRADED,
            reason=REASON_ENV_MISSING,
            missing_env=("autonomy.credential-file <org>:" + host,),
        )
    return ProbeResult(
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
    "ProbeResult",
    "probe_v1",
    "probe_host_v1",
]
