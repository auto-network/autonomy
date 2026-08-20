"""auto.network link operation policy resolution.

Turns ``autonomy.capability.operation_policy`` rows into a prompt-vs-
delegated verdict for the link operation classes (``link.publish``,
``link.revoke``, ``link.delegate`` — spec ``graph://a17c8657-939`` §6.6).

Row key convention (mirrors commit-policy scoping, narrower wins):

    workspace:<workspace_id>:<operation_class>
    org:<org_slug>:<operation_class>

Payloads carry ``contract="link"``, ``operation=<publish|revoke|delegate>``,
``mode=<prompt|delegated>``. The default — no row, malformed row, missing
mode — is always ``prompt``: a prompt is a missing delegation hop (§8),
and misconfiguration must never silently grant promptless authority.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from . import settings_ops
from .schemas.commit_policy import (
    LINK_OPERATION_CLASSES,
    LINK_OPERATION_MODES,
    OPERATION_POLICY_REVISION,
    OPERATION_POLICY_SET_ID,
)


LINK_DEFAULT_MODE = "prompt"


class LinkPolicyError(ValueError):
    """Raised when a link policy query itself is malformed."""


@dataclass(frozen=True)
class ResolvedLinkOperationMode:
    """Outcome of a link-operation policy resolution."""

    operation_class: str  # e.g. "link.publish"
    mode: str  # "prompt" | "delegated"
    key: str  # settings key that decided it, or "built-in:prompt"
    source: str  # setting id, or "built-in"


def _effective_org_slug(org: Any) -> str | None:
    if isinstance(org, settings_ops._CallerOrgSentinel):
        # org-scope: request — the sentinel resolves through the one caller
        # resolver (explicit > per-request contextvar > None). No ambient
        # source: a caller with a scope passed it or bound it.
        from tools.graph import ops as _ops
        return _ops._resolve_org(None)
    return org


def _candidate_keys(
    operation_class: str,
    *,
    workspace_id: str | None,
    org_slug: str | None,
) -> list[str]:
    keys: list[str] = []
    if workspace_id:
        keys.append(f"workspace:{workspace_id}:{operation_class}")
    if org_slug:
        keys.append(f"org:{org_slug}:{operation_class}")
    return keys


def _mode_from_member(member: Any, operation_class: str) -> str:
    """Extract the mode from a policy row, failing CLOSED to prompt.

    A row that names the wrong contract/operation for its key, or carries
    an unknown mode, is misconfigured — it resolves to ``prompt`` rather
    than being skipped, so a broken row can never be shadowed into a
    grant of delegated authority by a lower-precedence row either.
    """
    payload = getattr(member, "payload", None)
    if not isinstance(payload, dict):
        return LINK_DEFAULT_MODE
    contract, _, operation = operation_class.partition(".")
    if payload.get("contract") != contract or payload.get("operation") != operation:
        return LINK_DEFAULT_MODE
    mode = payload.get("mode", LINK_DEFAULT_MODE)
    if mode not in LINK_OPERATION_MODES:
        return LINK_DEFAULT_MODE
    return mode


def resolve_link_operation_mode(
    operation_class: str,
    *,
    workspace_id: str | None = None,
    org: Any = settings_ops.CALLER_ORG,
    members: Mapping[str, Any] | None = None,
) -> ResolvedLinkOperationMode:
    """Resolve prompt|delegated for one link operation class.

    Precedence: ``workspace:<id>:<class>`` beats ``org:<slug>:<class>``
    beats the built-in default (``prompt``). Pass *members* (a preloaded
    ``read_set(...).to_dict()`` map) to resolve without touching storage.
    """
    if operation_class not in LINK_OPERATION_CLASSES:
        raise LinkPolicyError(
            f"unknown link operation class {operation_class!r}; "
            f"valid: {LINK_OPERATION_CLASSES}"
        )
    if members is None:
        members = settings_ops.read_set(
            OPERATION_POLICY_SET_ID,
            org=org,
            peers=[],
            target_revision=OPERATION_POLICY_REVISION,
        ).to_dict()
    org_slug = _effective_org_slug(org)
    for key in _candidate_keys(
        operation_class, workspace_id=workspace_id, org_slug=org_slug,
    ):
        member = members.get(key)
        if member is None:
            continue
        return ResolvedLinkOperationMode(
            operation_class=operation_class,
            mode=_mode_from_member(member, operation_class),
            key=key,
            source=str(getattr(member, "id", "unknown")),
        )
    return ResolvedLinkOperationMode(
        operation_class=operation_class,
        mode=LINK_DEFAULT_MODE,
        key="built-in:prompt",
        source="built-in",
    )
